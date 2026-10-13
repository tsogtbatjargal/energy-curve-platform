"""The reviewed amendment and frozen pre-amendment boundary, with synthetic identifiers."""

import copy
import json
from pathlib import Path

# AmazonSSMManagedInstanceCore v2: only the agent registration and messaging actions.
# https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html
AGENT_ACTIONS = (
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ec2messages:AcknowledgeMessage",
    "ec2messages:DeleteMessage",
    "ec2messages:FailMessage",
    "ec2messages:GetEndpoint",
    "ec2messages:GetMessages",
    "ec2messages:SendReply",
)
BEFORE = json.loads(
    (Path(__file__).parent / "fixtures/bootstrap/boundary-before-session-manager.json").read_text()
)
AFTER = copy.deepcopy(BEFORE)
AFTER["Statement"][0]["Action"].extend(AGENT_ACTIONS)
