import boto3
import json
import re
from datetime import datetime, timezone, timedelta

REGION = "us-east-2"
ACCOUNT_ID = "728905193692"
SFN_ARN = "arn:aws:states:us-east-2:728905193692:stateMachine:letters_metadata_automation"
SCHEDULER_ROLE_ARN = "arn:aws:iam::728905193692:role/EventBridgeSchedulerRole"
SCHEDULE_GROUP = "cursive-debounce"
TABLE_NAME = "CursiveDebounce"
DEBOUNCE_MINUTES = 5

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)
scheduler = boto3.client("scheduler", region_name=REGION)

def lambda_handler(event, context):
    key = event["detail"]["object"]["key"]
    bucket = event["detail"]["bucket"]["name"]

    # Extract letterId from "input/letter_001/page_01.jpg" → "letter_001"
    match = re.match(r"input/([^/]+)/", key)
    if not match:
        print(f"Skipping — could not parse letterId from key: {key}")
        return {"status": "skipped", "key": key}

    letter_id = match.group(1)
    print(f"Detected upload for letterId: {letter_id}, key: {key}")

    # Upsert into DynamoDB
    table.put_item(Item={
        "letterId": letter_id,
        "bucket": bucket,
        "lastSeenAt": datetime.now(timezone.utc).isoformat()
    })

    # Delete existing schedule to reset the 5-min clock
    schedule_name = f"cursive-{letter_id}"
    try:
        scheduler.delete_schedule(
            Name=schedule_name,
            GroupName=SCHEDULE_GROUP
        )
        print(f"Reset timer — deleted existing schedule for {letter_id}")
    except scheduler.exceptions.ResourceNotFoundException:
        print(f"No existing schedule found for {letter_id}, creating fresh one")

    # Create new schedule: fire in 5 minutes
    fire_at = datetime.now(timezone.utc) + timedelta(minutes=DEBOUNCE_MINUTES)
    fire_at_str = fire_at.strftime("%Y-%m-%dT%H:%M:%S")

    scheduler.create_schedule(
        Name=schedule_name,
        GroupName=SCHEDULE_GROUP,
        ScheduleExpression=f"at({fire_at_str})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": SFN_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({
                "letterId": letter_id,
                "primaryImageKey": key,
                "imageKeys": [],
                "intermediateBucket": bucket
            })
        },
        ActionAfterCompletion="DELETE"
    )

    print(f"Scheduled Step Function for {letter_id} to fire at {fire_at_str} UTC")
    return {"status": "scheduled", "letterId": letter_id, "fireAt": fire_at_str}
