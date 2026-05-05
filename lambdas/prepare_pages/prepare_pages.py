import boto3
import json
import os
import re

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET  = "cursive-letters-pipeline"
VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}


def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def lambda_handler(event, context):
    letter_id = event.get('letterId')
    if not letter_id:
        raise ValueError("Missing required field: letterId")

    prefix    = f"input/{letter_id}/"
    paginator = s3.get_paginator('list_objects_v2')
    raw_keys  = []

    for page in paginator.paginate(Bucket=PIPELINE_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if os.path.splitext(key)[1].lower() in VALID_EXTENSIONS:
                raw_keys.append(key)

    if not raw_keys:
        raise ValueError(
            f"No valid image pages found for letterId='{letter_id}' "
            f"under prefix '{prefix}'"
        )

    raw_keys.sort(key=lambda k: natural_sort_key(os.path.basename(k)))
    primary_image_key = raw_keys[0]

    image_keys = [
        {
            "imageKey":        key,
            "pageKey":         os.path.splitext(os.path.basename(key))[0],
            "letterId":        letter_id,
            "primaryImageKey": primary_image_key
        }
        for key in raw_keys
    ]

    print(f"[OK] '{letter_id}' — {len(image_keys)} page(s): {[i['pageKey'] for i in image_keys]}")

    return {
        "letterId":        letter_id,
        "primaryImageKey": primary_image_key,
        "imageKeys":       image_keys
    }
