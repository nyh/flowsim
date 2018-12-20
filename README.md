# Flowsim
## Introduction
This project is a simulator for Scylla's write flow control algorithms.

Flowsim can generate graphs of the sort we showed in the blog post
https://www.scylladb.com/2018/12/04/worry-free-ingestion-flow-control/
which demonstrate how Scylla flow-controls, i.e., slows down, a client 
by delaying reponses to it, in order to ensure that queues of background
work do not grow out of bound (the aforementioned blog post explains
what these queues are).

Flowsim simulates a client with a fixed, or changing over time, concurrency
which makes write requests to a coordinator backed by N base replicas, and
when materialized views are involved, N view replicas. Each of those simulated
nodes is configured with an inherent speed (how many requests it can complete
each second), and we simulate the requests, when they complete, and how the
different flow control algorithms cause the client to be delayed, and the
simulator allows us to graph the client's performance over time, the length
of various queues (that the flow-control algorithms try to control), and so
on.

This is just a simulator written in Python, working in simulated time and
normally completing a simulation in just a few seconds; It does *not*
involve setting up any actual nodes or running Scylla.

The advantage of this simulator over running an actual Scylla is two-fold:
First, it allows to easily experiment with many different flow-control
algorithms, with much shorter turnaround times (running and hacking Scylla
is usually much harder and slower). Second, it allows to easily simulate
hard-to-reproduce scenarios, such as what happens when one node is 1%
slower than other nodes; At the same time it avoids the "noise" usually
inherent in benchmarking real setups and results in easier to understand
and more reproducable graphs.

Flowsim cannot currently simulate all aspects of a Scylla cluster. The most
glaring omission is that we currently treat the coordinator, the base
replicas, and view replicas, as separate nodes. In reality, these are all
on the same Scylla nodes, and all compete for resources (CPU and disk).
This can lead to flow-control issues that we can see in practice but the
simulator cannot currently simulate. For example, if for some reason a
node ends up dedicating 99% of its resources to finishing view updates,
and only 1% to finishing base-table writes, the base table writes can
become very slow. The simulator *can* simulate this occurance (we control
the base and view speeds separately, so can say that the base's speed 
is 0.01 times that of the view speed), but it has no way of figuring out
this this proportion is what will occur (1% vs 99%). To avoid this from
occuring in the first place, Scylla should probably use scheduler classes
to control exactly what percentage of the resources each of these two
(base or view writes) can take.

## How to use Flowsim
```
./flowsim.py examples/mv_linear1
xv out/*.png
```

