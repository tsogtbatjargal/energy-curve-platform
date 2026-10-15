"""Diagram of the optional temporary demo environment (PLAN.md M6): synthetic data only,
reachable from one owner /32, destroyed after use. Its Terraform is not part of this repository.

Regenerate: toolbox run -c ecp-diagrams ~/.local/share/ecp-diagrams-venv/bin/python -I demo.py
No IPs, DNS names or IDs belong here.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR, ECS, Fargate
from diagrams.aws.database import RDS, ElasticacheForRedis
from diagrams.aws.management import CloudwatchLogs, SystemsManager
from diagrams.aws.network import ALB, NATGateway
from diagrams.aws.security import IAMRole, SecretsManager
from diagrams.onprem.client import User

GRAPH = {
    "fontsize": "22",
    "labelloc": "t",
    "pad": "0.5",
    "nodesep": "0.9",
    "ranksep": "1.3",
    "splines": "ortho",
    "newrank": "true",
}
NODE = {"fontsize": "13", "height": "1.6", "margin": "0.0,0.3"}
EDGE = {"fontsize": "12", "color": "#555555"}

REQUEST = {"color": "#1f6feb", "penwidth": "2.2", "fontcolor": "#1f6feb"}
OUTBOUND = {"style": "dashed", "color": "#8a8a8a", "fontcolor": "#6a6a6a"}
ADMIN = {"style": "dotted", "color": "#9a6700", "fontcolor": "#9a6700"}

with Diagram(
    "Demo environment: synthetic data, owner-only access",
    filename="demo",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH,
    node_attr=NODE,
    edge_attr=EDGE,
):
    owner = User("owner\n(one /32 only)")

    with Cluster("AWS ca-central-1: demo VPC, 2 AZs"):
        with Cluster("public subnets"):
            alb = ALB("load balancer\nHTTP:80 from owner /32")
            nat = NATGateway("NAT gateway\nthe only way out:\nall private outbound 443")

        with Cluster("private subnets: app"):
            bastion = SystemsManager("bastion\nno public IP,\nno inbound rules")
            api = Fargate("API on Fargate\n1 task, read-only fs")
            bootstrap = ECS("one-off bootstrap task\n(ran once: schema +\nsynthetic import)")

        with Cluster("private subnets: data"):
            db = RDS("RDS Postgres 17\nsingle-AZ, not public")
            cache = ElasticacheForRedis("ElastiCache Valkey 9.1\nTLS")

    with Cluster("AWS regional services (reached through the NAT)"):
        ecr = ECR("ECR\nimage pinned by digest")
        secret = SecretsManager("Secrets Manager\nDB password,\ncreated by RDS")
        logs = CloudwatchLogs("CloudWatch Logs\n7 days")

    with Cluster("IAM roles: every one has the permissions boundary"):
        task_role = IAMRole("task role\nAPI + bootstrap:\nread own DB secret only")
        exec_role = IAMRole("execution role\npull image, write logs")
        bastion_role = IAMRole("bastion role\n11 Session Manager actions")

    # The user request path (blue).
    owner >> Edge(xlabel="HTTP:80", **REQUEST) >> alb
    alb >> Edge(label="port 8000", **REQUEST) >> api
    api >> Edge(label="SQL over TLS", **REQUEST) >> db
    api >> Edge(label="cache over TLS", **REQUEST) >> cache

    # One-time data load.
    bootstrap >> Edge(label="migrate + import\n(once)", style="bold") >> db

    # Admin path: Session Manager, no inbound ports; the DB and cache groups admit the bastion.
    owner >> Edge(label="Session Manager\n(no open ports)", **ADMIN) >> bastion
    bastion >> Edge(xlabel="admin only", **ADMIN) >> db

    # Outbound HTTPS from the private subnets goes through the one NAT. That is stated on the NAT's
    # label instead of drawn three times, which only crossed the request path.
    nat >> Edge(label="pull image", **OUTBOUND) >> ecr
    nat >> Edge(label="GetSecretValue", **OUTBOUND) >> secret
    nat >> Edge(label="logs", **OUTBOUND) >> logs

    # RDS owns its password.
    db >> Edge(label="creates and stores", style="dotted") >> secret

    # Roles stay as a legend, kept beside the services they unlock.
    secret - Edge(style="invis") - task_role
    task_role - Edge(style="invis") - exec_role - Edge(style="invis") - bastion_role
