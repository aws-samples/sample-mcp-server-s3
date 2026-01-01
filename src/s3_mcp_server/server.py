import asyncio
from importlib.resources import contents

import boto3
from mcp.server.models import InitializationOptions
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from dotenv import load_dotenv
import logging
import os
from typing import List, Optional, Dict
from mcp.types import Resource, LoggingLevel, EmptyResult, Tool, TextContent, ImageContent, EmbeddedResource, BlobResourceContents, ReadResourceResult

from .resources.s3_resource import S3Resource
from pydantic import AnyUrl

import base64

# Initialize server
server = Server("s3_service")

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_s3_server")

# Get max buckets from environment or use default
max_buckets = int(os.getenv('S3_MAX_BUCKETS', '5'))

# Initialize S3 resource
s3_resource = S3Resource(
    region_name=os.getenv('AWS_REGION', 'us-east-1'),
    max_buckets=max_buckets
)

boto3_s3_client = boto3.client('s3')

@server.set_logging_level()
async def set_logging_level(level: LoggingLevel) -> EmptyResult:
    logger.setLevel(level.lower())
    await server.request_context.session.send_log_message(
        level="info",
        data=f"Log level set to {level}",
        logger="mcp_s3_server"
    )
    return EmptyResult()

@server.list_resources()
async def list_resources(start_after: Optional[str] = None) -> List[Resource]:
    """
    List S3 buckets and their contents as resources with pagination
    Args:
        start_after: Start listing after this bucket name
    """
    resources = []
    logger.debug("Starting to list resources")
    logger.debug(f"Configured buckets: {s3_resource.configured_buckets}")

    try:
        # Get limited number of buckets
        buckets = await s3_resource.list_buckets(start_after)
        logger.debug(f"Processing {len(buckets)} buckets (max: {s3_resource.max_buckets})")

        # limit concurrent operations
        async def process_bucket(bucket):
            bucket_name = bucket['Name']
            logger.debug(f"Processing bucket: {bucket_name}")

            try:
                # List objects in the bucket with a reasonable limit
                objects = await s3_resource.list_objects(bucket_name, max_keys=1000)

                for obj in objects:
                    if 'Key' in obj and not obj['Key'].endswith('/'):
                        object_key = obj['Key']
                        mime_type = "text/plain" if s3_resource.is_text_file(object_key) else "text/markdown"

                        resource = Resource(
                            uri=f"s3://{bucket_name}/{object_key}",
                            name=object_key,
                            mimeType=mime_type
                        )
                        resources.append(resource)
                        logger.debug(f"Added resource: {resource.uri}")

            except Exception as e:
                logger.error(f"Error listing objects in bucket {bucket_name}: {str(e)}")

        # Use semaphore to limit concurrent bucket processing
        semaphore = asyncio.Semaphore(3)  # Limit concurrent bucket processing
        async def process_bucket_with_semaphore(bucket):
            async with semaphore:
                await process_bucket(bucket)

        # Process buckets concurrently
        await asyncio.gather(*[process_bucket_with_semaphore(bucket) for bucket in buckets])

    except Exception as e:
        logger.error(f"Error listing buckets: {str(e)}")
        raise

    logger.info(f"Returning {len(resources)} resources")
    return resources



@server.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """
    Read content from an S3 resource and return structured response

    Returns:
        Dict containing 'contents' list with uri, mimeType, and text for each resource
    """
    uri_str = str(uri)
    logger.debug(f"Reading resource: {uri_str}")

    if not uri_str.startswith("s3://"):
        raise ValueError("Invalid S3 URI")

    # Parse the S3 URI
    from urllib.parse import unquote
    path = uri_str[5:]  # Remove "s3://"
    path = unquote(path)  # Decode URL-encoded characters
    parts = path.split("/", 1)

    if len(parts) < 2:
        raise ValueError("Invalid S3 URI format")

    bucket_name = parts[0]
    key = parts[1]

    logger.debug(f"Attempting to read - Bucket: {bucket_name}, Key: {key}")

    try:
        response = await s3_resource.get_object(bucket_name, key)
        content_type = response.get("ContentType", "")
        logger.debug(f"Read MIMETYPE response: {content_type}")

        # Content type mapping for specific file types
        content_type_mapping = {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "application/markdown",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "application/csv",
            "application/vnd.ms-excel": "application/csv"
        }

        # Check if content type needs to be modified
        export_mime_type = content_type_mapping.get(content_type, content_type)
        logger.debug(f"Export MIME type: {export_mime_type}")

        if 'Body' in response:
            if isinstance(response['Body'], bytes):
                data = response['Body']
            else:
                # Handle streaming response
                async with response['Body'] as stream:
                    data = await stream.read()

            # Process the data based on file type
            if s3_resource.is_text_file(key):
                # text_content = data.decode('utf-8')
                text_content = base64.b64encode(data).decode('utf-8')

                return text_content
            else:
                text_content = str(base64.b64encode(data))

                result = ReadResourceResult(
                    contents=[
                        BlobResourceContents(
                            blob=text_content,
                            uri=uri_str,
                            mimeType=export_mime_type
                        )
                    ]
                )

                logger.debug(result)

                return text_content



        else:
            raise ValueError("No data in response body")

    except Exception as e:
        logger.error(f"Error reading object {key} from bucket {bucket_name}: {str(e)}")
        if 'NoSuchKey' in str(e):
            try:
                # List similar objects to help debugging
                objects = await s3_resource.list_objects(bucket_name, prefix=key.split('/')[0])
                similar_objects = [obj['Key'] for obj in objects if 'Key' in obj]
                logger.debug(f"Similar objects found: {similar_objects}")
            except Exception as list_err:
                logger.error(f"Error listing similar objects: {str(list_err)}")
        raise ValueError(f"Error reading resource: {str(e)}")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="ListBuckets", # https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListBuckets.html
            description="Returns a list of all buckets owned by the authenticated sender of the request. To grant IAM permission to use this operation, you must add the s3:ListAllMyBuckets policy action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_after": {"type": "string", "description": "Start listing after this bucket name"}
                },
                "required": [],
            },
        ),
        Tool(
            name="ListObjectsV2", # https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
            description="Returns some or all (up to 1,000) of the objects in a bucket with each request. You can use the request parameters as selection criteria to return a subset of the objects in a bucket. To get a list of your buckets, see ListBuckets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bucket_name": {"type": "string", "description": "When you use this operation with a directory bucket, you must use virtual-hosted-style requests in the format Bucket_name.s3express-az_id.region.amazonaws.com. Path-style requests are not supported. Directory bucket names must be unique in the chosen Availability Zone. Bucket names must follow the format bucket_base_name--az-id--x-s3 (for example, DOC-EXAMPLE-BUCKET--usw2-az1--x-s3)."},
                    "prefix": {"type": "string", "description": "the prefix of the keys to list."},
                    "max_keys": {"type": "integer", "description": "Sets the maximum number of keys returned in the response. By default, the action returns up to 1,000 key names. The response might contain fewer keys but will never contain more."}
                },
                "required": ["bucket_name"],
            },
        ),
        Tool(
            name="GetObject", # https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html
            description="Retrieves an object from Amazon S3. In the GetObject request, specify the full key name for the object. General purpose buckets - Both the virtual-hosted-style requests and the path-style requests are supported. For a virtual hosted-style request example, if you have the object photos/2006/February/sample.jpg, specify the object key name as /photos/2006/February/sample.jpg. For a path-style request example, if you have the object photos/2006/February/sample.jpg in the bucket named examplebucket, specify the object key name as /examplebucket/photos/2006/February/sample.jpg. Directory buckets - Only virtual-hosted-style requests are supported. For a virtual hosted-style request example, if you have the object photos/2006/February/sample.jpg in the bucket named examplebucket--use1-az5--x-s3, specify the object key name as /photos/2006/February/sample.jpg. Also, when you make requests to this API operation, your requests are sent to the Zonal endpoint. These endpoints support virtual-hosted-style requests in the format https://bucket_name.s3express-az_id.region.amazonaws.com/key-name . Path-style requests are not supported.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bucket_name": {"type": "string", "description": "Directory buckets - When you use this operation with a directory bucket, you must use virtual-hosted-style requests in the format Bucket_name.s3express-az_id.region.amazonaws.com. Path-style requests are not supported. Directory bucket names must be unique in the chosen Availability Zone. Bucket names must follow the format bucket_base_name--az-id--x-s3 (for example, DOC-EXAMPLE-BUCKET--usw2-az1--x-s3)."},
                    "key": {"type": "string", "description": "Key of the object to get. Length Constraints: Minimum length of 1."},
                    "max_retries": {"type": "string", "description": "max number of attempts to download the file."},
                },
                "required": ["Bucket", "Key"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[TextContent | ImageContent | EmbeddedResource]:
    logger.info(f"handle_call_tool got name {name}, args {arguments}")
    try:
        match name:
            case "ListBuckets":
                start_after = arguments.get("StartAfter", None) if arguments else None
                buckets = await s3_resource.list_buckets(start_after)
                logger.info(f"listBuckets returning buckets {buckets}")
                return [
                    TextContent(
                        type="text",
                        text=str(buckets)
                    )
                ]
            case "ListObjectsV2":
                args = {
                    "bucket_name": arguments['bucket_name']
                }
                if 'prefix' in arguments:
                    args['prefix'] = arguments['prefix']
                if "max_retries" in arguments:
                    args['max_retries'] = arguments['max_retries']

                objects = await s3_resource.list_objects(**args)

                logger.info(f"ListObjectsV2 returning objects {objects}")

                return [
                    TextContent(
                        type="text",
                        text=str(objects)
                    )
                ]
            case "GetObject":
                args = {
                    "bucket_name": arguments['bucket_name'],
                    "key": arguments['key']
                }
                if "max_retries" in arguments:
                    args['max_retries'] = arguments['max_retries']
                response = await s3_resource.get_object(**args)
                logger.info(f"GetObject got response {response}")
                file_content = response['Body'].read().decode('utf-8')
                logger.info(f"GetObject got file_content {file_content}")
                return [
                    TextContent(
                        type="text",
                        text=str(file_content)
                    )
                ]
    except Exception as error:
        return [
            TextContent(
                type="text",
                text=f"Error: {str(error)}"
            )
        ]

async def main():
    # Run the server using stdin/stdout streams
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="s3-mcp-server",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())