"""
Utility functions for webhook payload processing.

This module provides helper functions for safely parsing and handling
custom parameters from webhook payloads and coordinating server provisioning.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Union, Tuple, List

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse

from .config import logger
from . import config, models
from .services import security, kubernetes, notification

# Constants for event types
EVENT_START = 'EVENT_START'
EVENT_END = 'EVENT_END'
EVENT_DELETED = 'EVENT_DELETED'


def parse_custom_parameters(custom_params_str: Optional[str]) -> Dict[str, Any]:
    """
    Safe parsing of custom parameters from webhook payload.
    
    Args:
        custom_params_str: JSON serialized string of custom parameters
        
    Returns:
        Dictionary with custom parameters or empty dict if not present/invalid
    """
    if not custom_params_str:
        return {}
    
    try:
        return json.loads(custom_params_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Error parsing customParameters: {e}")
        return {}


def get_custom_parameter(custom_params: Dict[str, Any], key: str, default: Any = None) -> Any:
    """
    Get a specific custom parameter with default value.
    
    Args:
        custom_params: Dictionary of custom parameters
        key: Key of the parameter to get
        default: Default value if parameter doesn't exist
        
    Returns:
        Parameter value or default value
    """
    return custom_params.get(key, default)


def has_custom_parameters(custom_params_str: Optional[str]) -> bool:
    """
    Check if valid custom parameters are present.
    
    Args:
        custom_params_str: JSON serialized string of custom parameters
        
    Returns:
        True if valid custom parameters are present, False otherwise
    """
    if not custom_params_str:
        return False
    
    custom_params = parse_custom_parameters(custom_params_str)
    return bool(custom_params)


def parse_timestamp(timestamp_str: str) -> datetime:
    """
    Parse timestamp string to datetime object.
    
    Args:
        timestamp_str: ISO format timestamp string
        
    Returns:
        datetime object
        
    Raises:
        ValueError: If timestamp format is invalid
    """
    try:
        # Replace Z with timezone offset
        timestamp_str = timestamp_str.replace('Z', '+00:00')
        
        # Handle nanosecond precision by truncating to microseconds
        if '.' in timestamp_str:
            # Find the decimal point and truncate fractional seconds to 6 digits
            if '+' in timestamp_str:
                # Has timezone
                datetime_part, tz_part = timestamp_str.rsplit('+', 1)
                date_time, fractional = datetime_part.rsplit('.', 1)
                # Truncate fractional seconds to 6 digits (microseconds)
                fractional = fractional[:6].ljust(6, '0')
                timestamp_str = f"{date_time}.{fractional}+{tz_part}"
            else:
                # No explicit timezone
                date_time, fractional = timestamp_str.rsplit('.', 1)
                # Truncate fractional seconds to 6 digits (microseconds)
                fractional = fractional[:6].ljust(6, '0')
                timestamp_str = f"{date_time}.{fractional}"
            
        return datetime.fromisoformat(timestamp_str)
    except ValueError as e:
        logger.error(f"Failed to parse timestamp '{timestamp_str}': {e}")
        raise ValueError(f"Invalid timestamp format: {timestamp_str}")


async def verify_webhook_signature(request: Request, signature: Optional[str]) -> bytes:
    """
    Verify webhook signature and return raw payload.
    
    Args:
        request: FastAPI request object
        signature: Signature from webhook header
        
    Returns:
        Raw payload bytes
        
    Raises:
        HTTPException: If signature verification fails
    """
    raw_payload = await request.body()
    
    if config.WEBHOOK_SECRET:
        if not security.verify_signature(raw_payload, signature):
            logger.warning("Webhook signature verification failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature"
            )
    
    return raw_payload


def create_success_response(action: str, resource_name: str, user_id: Optional[str]) -> JSONResponse:
    """Create a standardized success response for single event operations."""
    return JSONResponse({
        "status": "success",
        "message": f"Successfully {action}ed server '{resource_name}'",
        "userId": user_id
    })


def get_image_details(os_slug: Optional[str]) -> Tuple[str, str, str]:
    """
    Retrieves the image URL, checksum, and checksum type for a given OS slug.
    If no slug is provided or found, falls back to the default image.
    """
    # Fallback to default if no slug provided
    if not os_slug:
        return config.PROVISION_IMAGE, config.PROVISION_CHECKSUM, config.PROVISION_CHECKSUM_TYPE

    slug = os_slug.lower()
    
    # Retrieve from config dictionary (see config.py update)
    image_url = config.OS_IMAGES.get(slug, config.PROVISION_IMAGE)
    checksum = config.OS_CHECKSUMS.get(slug, config.PROVISION_CHECKSUM)
    checksum_type = config.PROVISION_CHECKSUM_TYPE

    logger.info(f"Selected image for OS '{os_slug}': {image_url}")
    return image_url, checksum, checksum_type


def handle_provision_event(
    payload: models.WebhookPayload,
    raw_payload: bytes
) -> bool:
    """
    Handle provisioning event for a single server resource. Returns True on success.
    Supports Multi-OS selection and Multiple SSH Keys.
    """
    resource_name = payload.resource_name
    event_id = payload.event_id
    webhook_id = str(payload.webhook_id)  # Ensure string for logging functions
    user_id = payload.user_id or "unknown"

    try:
        # 1. Determine Image URL based on OS selection
        image_url, checksum, checksum_type = get_image_details(payload.operating_system)

        # 2. Handle SSH Keys (List vs Single Legacy Key)
        ssh_keys_list = payload.ssh_keys if payload.ssh_keys else []
        
        # Fallback: if list is empty but legacy key exists, use it
        if not ssh_keys_list and payload.ssh_public_key:
            logger.info("No 'sshKeys' list found. Falling back to legacy 'sshPublicKey'.")
            ssh_keys_list = [payload.ssh_public_key]
            
        if not ssh_keys_list:
            logger.warning(f"No SSH keys provided for {resource_name}. Access might be restricted.")

        # 3. Call Kubernetes Service
        success = kubernetes.patch_baremetalhost(
            bmh_name=resource_name,
            image_url=image_url,
            ssh_keys=ssh_keys_list,  # Pass list to updated kubernetes module
            checksum=checksum,
            checksum_type=checksum_type,
            wait_for_completion=False,
            webhook_id=webhook_id,
            user_id=user_id,
            event_id=event_id,
            timeout=config.PROVISIONING_TIMEOUT
        )
        
        if success:
            # Send webhook log for successful initiation
            os_msg = f" (OS: {payload.operating_system})" if payload.operating_system else ""
            if not notification.send_webhook_log(
                webhook_id=webhook_id,
                event_type=EVENT_START,
                success=True,
                payload_data=json.dumps(payload.model_dump()),
                status_code=200,
                response=f"Provisioning initiated for server '{resource_name}'{os_msg}",
                retry_count=0,
                metadata={"resourceName": resource_name, "userId": user_id, "eventId": event_id}
            ):
                logger.warning(f"Failed to send webhook log for server '{resource_name}'")
        
            logger.info(f"[{EVENT_START}] Successfully initiated provisioning for server '{resource_name}'{os_msg} (Event ID: {event_id}). Monitoring in background.")
            return True
        else:
            logger.error(f"[{EVENT_START}] Failed to start provisioning for server '{resource_name}' (Event ID: {event_id}).")
            return False
            
    except Exception as e:
        logger.error(f"Error provisioning server '{resource_name}': {str(e)}")
        return False


def handle_deprovision_event(
    payload: Union[models.WebhookPayload, models.EventWebhookPayload],
    raw_payload: bytes
) -> bool:
    """
    Handle deprovisioning event for a single server resource. Returns True on success.
    """
    if isinstance(payload, models.WebhookPayload):
        resource_name = payload.resource_name
        event_id = payload.event_id
        webhook_id = str(payload.webhook_id)
        user_id = payload.user_id
    elif isinstance(payload, models.EventWebhookPayload):
        resource_name = payload.data.resource.name
        event_id = str(payload.data.id)
        webhook_id = payload.webhook_id
        user_id = payload.data.keycloak_id if payload.data else None
    else:
        logger.error("Invalid payload type for deprovisioning.")
        return False

    try:
        success = kubernetes.patch_baremetalhost(
            bmh_name=resource_name,
            image_url=None  # None triggers deprovisioning
        )
        
        if success:
            logger.info(f"[{EVENT_END}] Successfully initiated deprovisioning for server '{resource_name}' (Event ID: {event_id}).")
            
            # Send webhook log for successful deprovisioning
            if event_id and notification.send_webhook_log(
                webhook_id=webhook_id,
                event_type=EVENT_END,
                success=True,
                payload_data=json.dumps(payload.model_dump()),
                status_code=200,
                response=f"Deprovisioning completed for server '{resource_name}'",
                retry_count=0,
                metadata={"resourceName": resource_name, "userId": user_id, "eventId": event_id}
            ):
                logger.debug(f"Successfully sent webhook log for server '{resource_name}' deprovisioning")
            else:
                logger.warning(f"Failed to send webhook log for server '{resource_name}' deprovisioning")
                
        else:
            logger.error(f"[{EVENT_END}] Failed to deprovision server '{resource_name}' (Event ID: {event_id}).")
        
        return success
        
    except Exception as e:
        logger.error(f"Error deprovisioning server '{resource_name}': {str(e)}")
        return False