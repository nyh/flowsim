#!/usr/bin/python3

# Flowsim is a simulator for Scylla's write flow control algorithms.
#
# It simulates a client with a fixed, or changing over time, concurrency which
# makes write requests to a coordinator backed by N base replicas, and when
# materialized views are involved, also N view replicas. Each of those
# simulated nodes is configured with an inherent speed (how many requests it
# can complete each second), and we simulate the requests, when they complete,
# and how the different flow control algorithms cause the client to be
# delayed, and the simulator allows us to graph the client's performance
# over time, the length of various queues (that the flow-control algorithms
# try to control), and so on.
#
# This is just a simulator, working in simulated time and normally completing
# a simulation in just a few seconds; It does not involve setting up any
# actual nodes or running Scylla. The advantage of this simulator over
# running an actual Scylla is two-fold: First, it allows to easily experiment
# with many different flow-control algorithms, with much shorter turnaround
# times (running and hacking Scylla is usually much harder and slower).
# Second, it allows to easily simulate hard-to-reproduce scenarios, such
# as what happens when one node is 1% slower than other nodes; At the same
# time it avoids the "noise" usually inherent in benchmarking real setups
# and results in easier to understand and more reproducable graphs.
#
# Flowsim cannot currently simulate all aspects of a Scylla cluster. The most
# glaring omission is that we currently treat the coordinator, the base
# replicas, and view replicas, as separate nodes. In reality, these are all
# on the same Scylla nodes, and all compete for resources (CPU and disk).
# This can lead to flow-control issues that we can see in practice but the
# simulator cannot currently simulate.

from collections import deque
from random import shuffle, random
import subprocess

all_metrics = []
class metric:
    def __init__(self, name):
        self.fn = "out/" + name + ".dat"
        self.f = open(self.fn, "w")
        all_metrics.append(self)
    def out(self, t, value):
        self.f.write("%s %s\n" % (t, value))        

# A "replica" object is used to simulate a replica - a base-table replica
# or a view-table replica. On this object one can write() to start a
# write request, and tick() to advance to the next tick in the simulation.
class replica:
    # id:    Identification string for this replica (used just for metric
    #        file name).
    # speed: Number of write() calls that can be completed per tick.
    #        Speed may be floating point.
    # view_replica_speed: If non-zero, we create a "view replica", a new
    #        replica with id "v"+id, and every write to the base replica
    #        will result in an asynchrnous write to the view replicas as well.
    #        If view_replica_speed < speed, this can result in a ever growing
    #        queue, which we need to solve: Eventually, the coordinator write
    #        speed needs to decrease to the speed of the slowest view
    #        replica in the system).
    #        Note that this is a simplification of a real Scylla system.
    #        In a real Scylla system view replicas are on the same node
    #        as base replicas and coordinators, and share their resources
    #        meaning that view writes also cause the base-table write speed
    #        to decrease. But I think this simplification is good enough,
    #        and easier to control with the view_replica_speed parameter.
    def __init__(self, id, speed, view_replica_speed):
        self.id = id
        self.ntick = 0
        self.write_speed = speed
        self.completion = 0.0
        self.requests = deque()
        self.replies = deque()
        self.metric_pending = metric("replica_%s_write_queue" % (id))
        if view_replica_speed:
            self.view_replica = replica("v" + str(id), view_replica_speed, 0)
        else:
            self.view_replica = None
    def write(self, rid):
        self.requests.append(rid)
        if self.view_replica:
            self.view_replica.write(rid)
    # Each tick clears "cql_write_speed" writes from the queue.
    def tick(self):
        if self.requests:
            self.completion += self.write_speed;
            # A test - increase speed by 100% every 100,000 ticks.
            # self.completion += self.write_speed * (1.0+self.ntick/100000.0)
            while (self.completion >= 1.0):
                self.completion -= 1.0
                self.replies.append(self.requests.popleft())
        self.ntick += 1
        self.metric_pending.out(self.ntick, len(self.requests))
    def all_nodes(self):
        ret = set()
        ret.add(self)
        if self.view_replica:
            ret.add(self.view_replica)
        return ret

# A "coordinator" object is used to simulate a coordinator, which sends
# write requests it got to a fixed list of base replicas (which, in turn,
# may also send updates to view replicas). On this object one can cql_write()
# to perform a write request, and tick() to advance to the next tick in the
# simulation.
class coordinator:
    # write_CL is desired write consistency level. After CL replicas have
    # responded, the coordinator replies to client and moves this request to
    # "background" mode until the rest of the replicas have replied too.
    # But at any moment the number of ongoing writes in background is limited
    # by max_background_writes.
    def __init__(self, id, base_replicas, write_CL, max_background_writes):
        self.id = id
        self.base_replicas = base_replicas
        self.write_CL = write_CL
        self.max_background_writes = max_background_writes
        self.ntick = 0
        # ongoing_writes[rid] is the number of replica writes for rid that
        # haven't yet been replied. It starts with len(base_replicas) and
        # when gets to 0, it gets deleted from ongoing_writes.
        self.ongoing_writes = {}
        self.background_writes = set()
        # throttled writes are ongoing writes which reached CL and we
        # wanted to move them to background_writes, but couldn't because
        # background_writes reached its limit. When background_writes
        # becomes shorter, we'll immediately move some items from
        # throttled_writes to backround_writes.
        self.throttled_writes = set()
        # when delayed_reply[rid] is set, it means we wanted to reply to
        # request but decided to delay that reply until later. The content
        # of delayed_reply[rid] is the tick when to do this reply.
        # Note that the current code doesn't do real replies, it just counts
        # unreplied writes in unreplied_writes() - so the number of
        # delayed_reply[] items is added to unreplied_writes().
        # We use delayed_reply as a flow control mechanism to control MV
        # update backlog.
        self.delayed_reply = {}
        self.reset_stats()
        self.total_writes = 0
        self.metric_bg = metric("coordinator_%d_background_writes" % (id))
        self.metric_fg = metric("coordinator_%d_foreground_writes" % (id))
        self.metric_writes = metric("coordinator_%d_total_writes" % (id))
    def reset_stats(self):
        self.stat_nticks = 0
        self.stat_nwrites = 0
    # Number of unreplied, or "foreground" requests. A client with limited
    # concurrency shouldn't send more writes if unreplied_writes() is above
    # its concurrency limit.
    def unreplied_writes(self):
        return len(self.ongoing_writes) - len(self.background_writes) + len(self.delayed_reply)
    def cql_write(self, rid):
        for rep in self.base_replicas:
            rep.write(rid)
        self.ongoing_writes[rid] = len(self.base_replicas)
        self.stat_nwrites += 1
        self.total_writes += 1
    def mv_pressure(self):
        #return 0
        # Each of the view replicas (actually, replica shards) involved in this
        # request has a different amount of backlog, but we need one estimate
        # of pressure to convert into a delay. The best results come from taking
        # the *max* of the different queue lengths, which basically means we
        # will try to slow down the client to keep *that* queue under control
        # (which will typically cause the smaller queues to go down to zero).
        # Taking a *sum* of the different queue lengths is natural but NOT a
        # good idea: It can allow the largest queue to continue to grow while
        # a smaller queue shortens, giving the impression that we're fine
        # because the sum is no longer growing.
        ql = max(len(rep.view_replica.requests) if rep.view_replica else 0 for rep in self.base_replicas)
        #return ql

        # Add random jitter
        #ql = max(0, ql + (random()-0.5)*100)

        # Hack to only get an update of ql once every N ticks, not all the
        # time to check what happens if we use old ql information
        #if not hasattr(self, 'hack1_ntick') or self.ntick > self.hack1_ntick + 1000:
        #    self.hack1_ntick = self.ntick
        #    self.hack1_ql = ql
        #ql = self.hack1_ql
        
#        # Formula 1: Look for plateau in ret (lower or higher delays will
#        # result increasing or decreasing queue size, so we'll eventually
#        # converge on plateau (gradient descent method?).
#        # The ql we convege on is a function of the multiplier below - a higher
#        # multiplier will result in lower ql (this is obvious, because the
#        # *bp* of the plateau - when the queue no longer grows - is constant
#        # so ql * multiplier is constant).
#        bp = ql * 2.0
#        # Formula 2: Modify previous bp based on previous bp and measured
#        # queue size. An empty queue will cause us to slowly decrease bp
#        # and a larger queue will cause us to slowly increase it.
#        if not hasattr(self, 'prev_bp'):
#            self.prev_bp = 0
##        if ql > 3: # TODO threshold?
##            bp = self.prev_bp + 1
##        else:
##            bp = max(self.prev_bp - 1, 10)
#        # hack to only recalculate bp once per tick. Doesn't help anything.
#        if not hasattr(self, 'prev_ntick'): # hack try
#            self.prev_ntick = self.ntick # hack try
#        if self.prev_ntick == self.ntick: # hack try
#            return int(max(self.prev_bp,1)) # hack try
#        self.prev_ntick = self.ntick # hack try
#
##        # try: have prev_ql and see if the queue increases, not if it's empty!
##        if not hasattr(self, 'prev_ql'):
##            self.prev_ql = ql
#
#        if ql > 1:
#            # TODO: If the queue is high because of initial conditions,
#            # it will take a long time to drain it, and all that time, we'll
#            # remain at ql > 1 regardless of how we increase bp here.
#            # So watch out not to increase it too much while the queue
#            # is decreasing. I.e., let's not check if ql > 1 but rather
#            # whether ql > prev_ql!!!  Didn't work... How to fix?
#            bp = (self.prev_bp+1)*1.001
#        else:
#            bp = self.prev_bp*0.999
#        self.prev_ql = ql
#
        # Formula 3: like formula 1 but modifying the multiplicative constant
        # C. We have a desired stable queue length (dql), and if we've converged
        # on a ql > dql, we need to increase C to lower ql. And vice versa.
        dql = 200
        if not hasattr(self, 'C'):
            self.C = 1.0
        if abs(ql - dql)/dql < 0.1:
            # if ql is close enough (within 10%) to dql, then C is good
            # enough and we don't continue to improve it. This will save
            # us from oscillations around the perfect C and of the queue
            # length, and therefore of the performance.
            pass
        elif ql > dql:
            self.C = self.C * 1.001
        elif ql < dql and ql > 0:
            # Note: the queue not only becomes short if we increased C too
            # much - it can also becomes short if the server is not in full
            # utilization! I'm not sure how to solve that... But definitely
            # if the queue is completely empty, there is no point in changing
            # C because a zero ql will return 0 regardless of change to C.
            self.C = self.C * 0.999
        bp = self.C * ql

        # Formula 4: Same as formula 3, but instead of using ql linearly,
        # use ql^E for some exponent E. The thinking is that while the delay
        # we calculate, bp, may need to change through several orders of
        # magnitude (as the client concurrency changes orders of magnitude),
        # we want the queue length to change less.
        # We divide the formula also by dql**(E-1) otherwise the starting C=1.0
        # is ridiculously high. This is easier to understand by unit analysis,
        # we use the unit-less ql/dql in the polynomial instead of unit-full
        # ql, and multiply the whole thing by dql to get the right units.
        #E = 3.0
        #bp = dql * self.C * (ql/dql)**E

        #bp = 1.0 * ql
 
#        self.prev_bp = bp
        return bp
    # Call delayed_reply() after already "replying" (a connection is "replied"
    # when it is put in background_writes, or deleted completely from
    # ongoing_writes). This will cause unreplied_writes() to continue counting
    # this connection as unreplied for a while longer. The length of the
    # while is determined by mv_pressure().
    def delay_reply(self, rid):
        delay = self.mv_pressure()
        # Add random jitter in (-500,500):
        #delay += (random()-0.5)*2000
        # Random multiplicative jitter
        #delay *= 1 + random()
        # Add one-side random jitter in (0,500)
        #delay += random()*500
        # Add one-side random jitter in (0,500)
        #delay -= random()*500
        # Add consistent error, positive or negative. Does not make any
        # difference because if we do delay -= 500 here, the algorithm just
        # converges to the delay that when 500 is subtracted from it, is
        # the right delay we need!
        # delay += 500

        # Note that to set the delayed_reply array, the delay must be a
        # positive integer.
        delay = int(delay)
        if delay <= 0:
            return
        print(delay)
        self.delayed_reply[rid] = self.ntick + delay
    def tick(self):
        throttling = len(self.background_writes) >= self.max_background_writes
        # If previously, while background writes reached its limit, we
        # moved requests to throttled_writes instead of background_writes,
        # and if now the background writes are below the limit, move as many
        # throttled writes as we can to the background_writes (in other words,
        # reply to these requests)
        while not throttling and self.throttled_writes:
            # note that this pops a random request from throttled_writes.
            # it doesn't make any FIFO guarantee.
            rid = self.throttled_writes.pop()
            self.background_writes.add(rid)
            self.delay_reply(rid)
            throttling = len(self.background_writes) >= self.max_background_writes
        # Execute delayed replies, if the time is right. Currently, we don't
        # really need to "reply" anything, just removing the delayed_reply
        # entry reduces the unreplied_writes() so tells the fixed-concurrency
        # client that it can send a new request.
        remove = []
        for key, value in self.delayed_reply.items():
            if value == self.ntick:
                remove.append(key)
        for key in remove:
            del self.delayed_reply[key]

        for rep in self.base_replicas:
            for rid in rep.replies:
                self.ongoing_writes[rid] -= 1
                if self.ongoing_writes[rid] == 0:
                    # Done with all replica writes. No longer ongoing write.
                    self.background_writes.discard(rid)
                    self.throttled_writes.discard(rid)
                    del self.ongoing_writes[rid]
                    # It is likely we already considered this write "replied"
                    # when it was already in background_writes, and if so
                    # delay_reply() was already called for it. In that case
                    # don't calculate a new delay.
                    if not rid in self.delayed_reply:
                        self.delay_reply(rid)
                elif len(self.base_replicas) - self.ongoing_writes[rid] == self.write_CL:
                    # This write reached CL and we can reply to the client
                    # immediately, if not throttling. replying to the client
                    # means adding this request id to background_writes, i.e,
                    # the write continues in the background after the reply.
                    if throttling:
                        # Remember that we wanted to reply to this write
                        # (move it to background_writes) but couldn't.
                        # We'll do it later when the number of background
                        # writes drops.
                        self.throttled_writes.add(rid)
                    else:
                        self.background_writes.add(rid)
                        self.delay_reply(rid) 
            rep.replies.clear()
        self.ntick += 1
        self.stat_nticks += 1
        self.metric_fg.out(self.ntick, self.unreplied_writes())
        self.metric_bg.out(self.ntick, len(self.background_writes))
        self.metric_writes.out(self.ntick, self.total_writes)
    def all_nodes(self):
        ret = set()
        ret.add(self)
        for rep in self.base_replicas:
            ret.update(rep.all_nodes())
        return ret


###############################################################################
#case=4
#if case==1:
#    # Test case for base-table only, three replicas with different speeds
#    # two.
#    b1 = replica(1, 0.1, 0)
#    b2 = replica(2, 0.09, 0)
#    b3 = replica(3, 0.08, 0)
#    c = coordinator(1, [b1, b2, b3], write_CL=2, max_background_writes=300)
#elif case==3:
#    # Test case for base-table only, three replicas one is slower than the other
#    # two.
#    b1 = replica(1, 0.1, 0)
#    b2 = replica(2, 0.1, 0)
#    b3 = replica(3, 0.099, 0)
#    c = coordinator(1, [b1, b2, b3], write_CL=2, max_background_writes=300)
#elif case==2:
#    # Test case for base-table and view. Some views finish faster than the
#    # base, some slower. Need to see everything is slowed down to the pace
#    # of the slowest view.
#    b1 = replica(1, 0.1, 0.06)
#    b2 = replica(2, 0.09, 0.04)
#    b3 = replica(3, 0.08, 0.11)
#    c = coordinator(1, [b1, b2, b3], write_CL=2, max_background_writes=300)
#elif case==4:
#    # Test case for base-table and view, based on case 3. All views are
#    # equally slow
#    b1 = replica(1, 0.1, 0.03)
#    b2 = replica(2, 0.1, 0.03)
#    b3 = replica(3, 0.099, 0.03)
#    c = coordinator(1, [b1, b2, b3], write_CL=2, max_background_writes=300)
#
#
###############################################################################

average_window_ticks = 2000
metric_avg_throughput = metric("coordinator_avg_throughput_%s_ticks" % (average_window_ticks))

def workload_variable_concurrency(c, client_concurrency, ticks):
    all_nodes = list(c.all_nodes())
    for i in range(int(ticks)):
        if c.unreplied_writes() < client_concurrency(i):
            c.cql_write(i)
        for node in all_nodes:
            node.tick()
        if i % average_window_ticks == 0:
            metric_avg_throughput.out(c.ntick, (c.stat_nwrites / c.stat_nticks))
            print("%s: average over last 2000 ticks: requests/tick: %s" % (i, c.stat_nwrites / c.stat_nticks))
            c.reset_stats()

def workload_fixed_concurrency(c, client_concurrency, ticks):
    workload_variable_concurrency(c, lambda t: client_concurrency, ticks)


# The workload
#workload_fixed_concurrency(c, 50, 200000)
#workload_fixed_concurrency(c, 50, 400000)

# Workload doubles concurrency in the middle. This can represent doubling
# the number of clients connecting, i.e., not a very common occurance
#workload_fixed_concurrency(c, 50, 100000)
#workload_fixed_concurrency(c, 100, 100000)

# A more likely occurance that the concurrency increases or decreases, but
# not drastically. Can represent a situation where several clients are
# already connected and have some total concurrency, and then a new client
# joins.
#workload_fixed_concurrency(c, 50, 100000)
#workload_fixed_concurrency(c, 55, 100000)

# Workload with sudden stop in the middle. The server "forgets" what it
# has done previously, and needs to find the right delay again.
#workload_fixed_concurrency(c, 50, 100000)
#workload_fixed_concurrency(c, 0, 10000)
#workload_fixed_concurrency(c, 50, 100000)

# workload with concurrency gradually increasing from 50 to 100
#workload_variable_concurrency(c, lambda t: 50+50*t/200000, 200000)

# workload oscillating slowly between 50 and 100 several times.
# The result doesn't look perfectly flat, but is reasonable - around
# the correct average and queue length is never far from the desired 200.
#from math import sin
#workload_variable_concurrency(c, lambda t: 50+sin(t/63662*2)*sin(t/63662*2)*50, 200000)


# FIXME: concurrency 500, doesn't work. Maybe can't aim to achieve
# dql=200, but 2000 also didn't work. What will?
#workload_fixed_concurrency(c, 500, 500000)

def flush_metrics():
    for metric in all_metrics:
        metric.f.flush()

def close_metrics():
    for metric in all_metrics:
        metric.f.close()
    all_metrics.clear()

def plot(out,cmd):
    # We use close_metrics() to flush and close all the data files so we
    # can plot them. This means one cannot continue the simulation after
    # the first plot.
    close_metrics()
    print("plotting %s" % (out))
    subprocess.run(['gnuplot'], encoding='UTF-8', input="""
    #set terminal png size 1400,800 enhanced font "Sans,18"
    set terminal png size 1400,800 enhanced font "Sans,22"
    set out "%s"
    """ % (out) + cmd)

# hz is the number of ticks per second. We want the graph to show
# seconds, and requests per second - not ticks.
def plot_throughput(hz, fn, misc):
    plot(fn, """
        set xlabel 'Time (seconds)'
        %s
        plot '%s' using ($1/%s):($2*%s) w lines lw 6 title 'Writes / second'
        """ % (misc, metric_avg_throughput.fn, hz,hz))

def plot_view_backlog(hz, b, fn, misc):
    plot(fn, """
    #set ylabel 'Pending view-update queue length'
    set xlabel 'Time (seconds)'
    %s
    # show only view replica 1
    plot '%s' using ($1/%s):($2) w lines lw 6 title 'View-update backlog'
    """ % (misc, b.view_replica.metric_pending.fn, hz))

def plot_background_writes(hz, c, fn, misc):
    plot(fn, """
        set xlabel 'Time (seconds)'
        %s
        plot '%s'  using ($1/%s):2 w l lw 3 title 'Background writes'
        """ % (misc, c.metric_bg.fn, hz))

import sys
exec(open(sys.argv[1]).read())


            
# Run Gnuplot to generate nice graphs

#for metric in all_metrics:
#    metric.f.flush()
#
#import subprocess
#def plot(out,cmd):
#    print("plotting %s" % (out))
#    subprocess.run(['gnuplot'], encoding='UTF-8', input="""
#    #set terminal png size 1400,800 enhanced font "Sans,18"
#    set terminal png size 1400,800 enhanced font "Sans,22"
#    set out "%s"
#    """ % (out) + cmd)
#
## TODO: consider moving graphs (and metrics) to have as X axis not
## the ticks but rather the request number, it's easier to understand.
#if case == 1:
#    plot("out/1.png", """
#        set ylabel 'Background writes'
#        set xlabel 'Time (unspecified ticks)'
#        plot '%s'  w l lw 3 title 'Background writes', '%s' w l lw 3
#        """ % (c.metric_bg.fn, b1.metric_pending.fn))
#    plot("out/2.png", """
#        set ylabel 'Throughput (writes served per tick)'
#        set xlabel 'Time (ticks)'
#        set ytics add ("replica 1: %s" %s)
#        set ytics add ("replica 2: %s" %s)
#        set ytics add ("replica 3: %s" %s)
#        set grid
#        set yrange [0:0.2]
#        plot '%s'  w lines lw 3 title 'Throughput'
#        """ % (b1.write_speed, b1.write_speed, b2.write_speed, b2.write_speed, b3.write_speed, b3.write_speed, metric_avg_throughput.fn))
#
#if case == 3:
#    # We do 0.1 writes per tick. To simulate 10,000 writes per second,
#    # each tick would be 1/100,000 of a second. Let's show on the graph
#    # seconds, or 100,000 ticks, so we need to multiply by 1/100,000
#    multiplier = 1/100000
#    plot("out/1.png", """
#        set ylabel 'Background writes'
#        set xlabel 'Time (seconds)'
#        #set xrange [0:1]
#        plot '%s'  using ($1*%s):2 w l lw 3 title ''
#        """ % (c.metric_bg.fn, multiplier))
#    plot("out/2.png", """
#        #set ylabel 'Throughput (writes per second)'
#        set xlabel 'Time (seconds)'
#        unset ytics
#        set ytics add ("replica 1, 2: %s" %s)
#        set ytics add ("replica 3: %s" %s)
#        #set grid
#        #set yrange [0:20000]
#        set yrange [9000:11000]
#        #set xrange [0:1]
#        plot '%s' using ($1*%s):($2*%s) w lines lw 6 title 'Writes / second'
#        """ % (round(b1.write_speed/multiplier), round(b1.write_speed/multiplier), round(b3.write_speed/multiplier), round(b3.write_speed/multiplier), metric_avg_throughput.fn, multiplier,1/multiplier))
#
#if case == 4:
#    # We do 0.1 writes per tick. To simulate 10,000 writes per second,
#    # each tick would be 1/100,000 of a second. Let's show on the graph
#    # seconds, or 100,000 ticks, so we need to multiply by 1/100,000
#    multiplier = 1/100000
#    plot("out/1.png", """
#        set ylabel 'Background writes'
#        set xlabel 'Time (seconds)'
#        #set xrange [0:1]
#        plot '%s'  using ($1*%s):2 w l lw 3 title ''
#        """ % (c.metric_bg.fn, multiplier))
#    plot("out/2.png", """
#        #set ylabel 'Throughput (writes per second)'
#        set xlabel 'Time (seconds)'
#        #unset ytics
#        #set ytics add ("replica 1, 2: %s" %s)
#        #set ytics add ("replica 3: %s" %s)
#        set ytics 0,1000
#        set grid
#        #set yrange [0:20000]
#        #set yrange [9000:11000]
#        set yrange [1000:11000]
#        #set xrange [0:1]
#        plot '%s' using ($1*%s):($2*%s) w lines lw 6 title 'Writes / second'
#        """ % (round(b1.write_speed/multiplier), round(b1.write_speed/multiplier), round(b3.write_speed/multiplier), round(b3.write_speed/multiplier), metric_avg_throughput.fn, multiplier,1/multiplier))
#    plot("out/3.png", """
#    set ylabel 'Pending view-update queue length'
#    set xlabel 'Time (seconds)'
#    # show only view replica 1
#    plot '%s' using ($1*%s):($2) w lines lw 6 title ''
#    """ % (b1.view_replica.metric_pending.fn, multiplier))
#
#
#if case == 2:
##    plot("out/1.png", """
##        set ylabel 'Background writes'
##        set xlabel 'Time (unspecified ticks)'
##        plot '%s'  w l lw 3 title 'Background writes', '%s' w l lw 3
##        """ % (c.metric_bg.fn, b1.metric_pending.fn))
#    plot("out/1.png", """
#        set ylabel 'Background writes'
#        set xlabel 'Time (unspecified ticks)'
#        plot '%s'  w l lw 3 title 'Background writes'
#        """ % (c.metric_bg.fn,))
#    plot("out/2.png", """
#        set ylabel 'Throughput (writes served per tick)'
#        set xlabel 'Time (ticks)'
#        set ytics add ("replica 1: %s" %s)
#        set ytics add ("replica 2: %s" %s)
#        set ytics add ("replica 3: %s" %s)
#        set ytics add ("slowest view: %s" %s)
#        set grid
#        set yrange [0:0.2]
#        plot '%s'  w lines lw 3 title 'Avg over %s ticks'
#        """ % (b1.write_speed, b1.write_speed, b2.write_speed, b2.write_speed, b3.write_speed, b3.write_speed, b2.view_replica.write_speed, b2.view_replica.write_speed, metric_avg_throughput.fn, average_window_ticks))
#    plot("out/3.png", """
#    set ylabel 'Pending view-update queue length'
#    set xlabel 'Time (ticks)'
#    plot '%s' w lines lw 3 title 'view replica 1', '%s'  w lines lw 3 title 'view replica 2', '%s'  w lines lw 3 title 'view replica 3'
#    """ % (b1.view_replica.metric_pending.fn, b2.view_replica.metric_pending.fn, b3.view_replica.metric_pending.fn))
#


# TODO: For ongoing_writes, also keep the tick when they started and then
# when it is replied (moved to background_write or dropped from ongoing_writes),
# write the latency of the reply as a metric (maybe a histogram-like metric?)
# Gleb suspects we have a problem of timeouts; Maybe in some situations we
# keep a client unresponded for a long time because its write to the slowest
# node is stuck in a very long queue.

