# Cloud resource Scaling

We built a small auto-scaling test setup from scratch. Basically, we have a fake web service, send different types of traffic to it (steady load, sudden spike, ramp-up, periodic, bursty), and check which scaling strategy handles it best.

We tested 5 strategies:

STATIC — does nothing, fixed replicas
CPU — scales on utilisation (like normal K8s HPA)
TREND — scales early when load is rising
LATENCY — scales when response time goes bad
WARPS — our idea; it picks the best strategy based on traffic pattern
We ran 152 experiments and got graphs + stats out of it.

Findings in short:

STATIC fails badly on spikes
CPU is okay but struggles on bursty traffic
TREND gives good SLA but uses too many replicas and oscillates
LATENCY is cheap but unreliable on periodic/bursty loads
WARPS is the most balanced — not best everywhere, but consistently good across all workloads
