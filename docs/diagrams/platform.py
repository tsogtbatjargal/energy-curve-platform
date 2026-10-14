"""Platform architecture diagram (docs/diagrams/README.md has the regeneration commands).

Every box is drawn from what the code and ADRs say exists; the ADR is cited beside it.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Glue, Redshift
from diagrams.aws.compute import ECR, ECS, Lambda
from diagrams.aws.integration import SNS, Eventbridge, StepFunctions
from diagrams.aws.management import CloudwatchAlarm
from diagrams.aws.security import IAMRole
from diagrams.aws.storage import S3
from diagrams.generic.storage import Storage
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import Client
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis
from diagrams.programming.framework import FastAPI
from diagrams.programming.language import Python

GRAPH = {"fontsize": "20", "labelloc": "t", "pad": "0.6", "nodesep": "1.1", "ranksep": "1.6"}
NODE = {"fontsize": "13", "height": "1.9", "margin": "0.0,0.35"}
EDGE = {"fontsize": "12"}
HIDDEN = Edge(style="invis")
SYNTHETIC = {"fontcolor": "#7a4b00", "bgcolor": "#fff8e6", "pencolor": "#d9a400"}

with Diagram(
    "energy-curve-platform: modelled curves from EIA inputs (cloud path is synthetic only)",
    filename="platform",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH,
    node_attr=NODE,
    edge_attr=EDGE,
):
    # ADR-0011: real EIA data exists only locally; the cloud path never sees it.
    with Cluster("Local (the only place real EIA data exists)"):
        eia = Client("EIA API\n(local only)")
        # ADR-0012 (ingestion), ADR-0002 (curve method): Polars pipeline and curve engine.
        pipeline = Python("pipeline +\ncurve engine")
        # ADR-0019: the same artifact-store interface locally and on S3.
        store = Storage("local artifact store\n(published pointer)")
        # ADR-0013 (Postgres 17, Valkey 9, serving), ADR-0014 (API, cache, events),
        # ADR-0016 (w2ui app).
        pg = PostgreSQL("Postgres")
        valkey = Redis("Valkey")
        api = FastAPI("FastAPI")
        app = Client("w2ui app")
        eia >> pipeline >> store
        store >> Edge(label="importer") >> pg
        api >> Edge(label="cache") >> valkey
        api >> pg
        api >> app

    with Cluster("AWS ca-central-1 (synthetic data only)", graph_attr=SYNTHETIC):
        # ADR-0022: the batch stack, ADR-0019: bronze/silver/gold and the published pointer.
        with Cluster("Daily batch (ADR-0022, ADR-0019)"):
            sched = Eventbridge("EventBridge\nScheduler")
            sfn = StepFunctions("Step Functions")
            stage = Lambda("Lambda\n(stage bronze)")
            task = ECS("ECS Fargate\n(pipeline)")
            data = S3("S3 data bucket\nbronze/silver/gold\n+ pointer")
            ecr = ECR("ECR\n(image by digest)")
            alarm = CloudwatchAlarm("CloudWatch alarm")
            topic = SNS("SNS alerts")
            # ADR-0022: the state machine runs Stage (Lambda) and then Pipeline (ECS runTask.sync).
            sched >> sfn
            sfn >> Edge(label="1 stage") >> stage
            sfn >> Edge(label="2 pipeline") >> task
            stage >> Edge(label="staging") >> data
            task >> Edge(label="publish") >> data
            ecr >> Edge(style="dashed") >> stage
            ecr >> Edge(style="dashed") >> task
            sfn >> Edge(style="dotted", label="failed") >> alarm >> topic

        # ADR-0023: Glue shape job (no VPC), Redshift Serverless in its own private VPC,
        # loaded by COPY from both buckets and queried through the Data API.
        with Cluster("On demand (ADR-0023)"):
            # jobs/glue/shape_job.py generates the synthetic history inside Spark; no input bucket.
            glue = Glue("Glue PySpark shape job\n(generates synthetic history)")
            glue_bucket = S3("S3 Glue bucket\nshape parameters")
            with Cluster("own private VPC, no internet path"):
                rs = Redshift("Redshift Serverless")
            glue >> glue_bucket
            glue_bucket >> Edge(label="COPY (shape file)") >> rs
            data >> Edge(xlabel="COPY (gold files)") >> rs

    # ADR-0010 (CI roles), ADR-0018 (workload boundary): OIDC plan and deploy roles.
    with Cluster("CI (GitHub Actions)"):
        gha = GithubActions("GitHub Actions")
        plan = IAMRole("plan role\n(read-only)")
        deploy = IAMRole("deploy role\n(approval)")
        gha >> Edge(label="OIDC") >> plan
        gha >> Edge(label="OIDC") >> deploy

    # Layout only: this puts the CI cluster beside the local stack instead of below it.
    pg >> HIDDEN >> gha
