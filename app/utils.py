"""
Utility functions for webhook payload processing.

This module provides helper functions for safely parsing and handling
custom parameters from webhook payloads and coordinating server provisioning.
STRICT MODE: No Fallbacks, Dynamic Provisioning Only.
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
    """
    if not custom_params_str:
        return {}
    
    try:
        return json.loads(custom_params_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Error parsing customParameters: {e}")
        return {}


def get_custom_parameter(custom_params: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Get a specific custom parameter value safely."""
    return custom_params.get(key, default)


def has_custom_parameters(custom_params_str: Optional[str]) -> bool:
    """Check if valid custom parameters are present."""
    if not custom_params_str:
        return False
    
    custom_params = parse_custom_parameters(custom_params_str)
    return bool(custom_params)


def parse_timestamp(timestamp_str: str) -> datetime:
    """
    Parse timestamp string to datetime object.
    """
    try:
        # Replace Z with timezone offset
        timestamp_str = timestamp_str.replace('Z', '+00:00')
        
        # Handle nanosecond precision by truncating to microseconds
        if '.' in timestamp_str:
            if '+' in timestamp_str:
                datetime_part, tz_part = timestamp_str.rsplit('+', 1)
                date_time, fractional = datetime_part.rsplit('.', 1)
                fractional = fractional[:6].ljust(6, '0')
                timestamp_str = f"{date_time}.{fractional}+{tz_part}"
            else:
                date_time, fractional = timestamp_str.rsplit('.', 1)
                fractional = fractional[:6].ljust(6, '0')
                timestamp_str = f"{date_time}.{fractional}"
            
        return datetime.fromisoformat(timestamp_str)
    except ValueError as e:
        logger.error(f"Failed to parse timestamp '{timestamp_str}': {e}")
        raise ValueError(f"Invalid timestamp format: {timestamp_str}")


async def verify_webhook_signature(request: Request, signature: Optional[str]) -> bytes:
    """
    Verify webhook signature and return raw payload.
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


def handle_provision_event(
    payload: models.WebhookPayload,
    raw_payload: bytes
) -> bool:
    """
    Handle provisioning event for a single server resource.
    STRICT MODE: Requires 'imageUrl' and 'checksumUrl' from Frontend.
    No fallbacks allowed.
    """
    resource_name = payload.resource_name
    event_id = payload.event_id
    webhook_id = str(payload.webhook_id)
    user_id = payload.user_id or "unknown"

    try:
        # 1. STRICT VALIDATION: Check if dynamic image data is present
        if not payload.image_url or not payload.checksum_url:
            msg = f"PROVISIONING FAILED: Missing 'imageUrl' or 'checksumUrl' for {resource_name}. No defaults allowed."
            logger.error(msg)
            
            # Send failure notification
            notification.send_webhook_log(
                webhook_id=webhook_id, 
                event_type=EVENT_START, 
                success=False,
                payload_data=json.dumps(payload.model_dump()), 
                response=msg,
                metadata={"error": "Missing Mandatory Image Config"}
            )
            return False

        # 2. Extract Data (No Magic)
        image_url = payload.image_url
        checksum = payload.checksum_url
        
        # --- LOGICA DI PARSING DINAMICO DEL FORMATO ---
        # Verifichiamo l'estensione dell'URL per impostare il formato corretto
        url_lower = image_url.lower()
        if url_lower.endswith('.qcow2'):
            detected_format = "qcow2"
        elif url_lower.endswith('.vmdk'):
            detected_format = "vmdk"
        elif url_lower.endswith('.iso'):
            detected_format = "iso"
        else:
            detected_format = "raw"  # Default per .img, .bin o se l'estensione manca

        # Priorità: Formato esplicito dal Frontend > Formato detectato dall'URL
        image_format = payload.image_format if payload.image_format else detected_format
        # ----------------------------------------------

        ssh_keys_list = payload.ssh_keys if payload.ssh_keys else []
        
        # Checkpoint log con il formato rilevato
        logger.info(f"Provisioning '{resource_name}' with explicit URL: {image_url} (Detected Format: {image_format})")

        if not ssh_keys_list:
            logger.warning(f"No SSH keys provided for {resource_name}. Access might be restricted.")

        # 3. Call Kubernetes Service
        success = kubernetes.patch_baremetalhost(
            bmh_name=resource_name,
            image_url=image_url,
            ssh_keys=ssh_keys_list,
            checksum=checksum,
            checksum_type="sha256", # Assumiamo sha256 per i link diretti
            image_format=image_format,
            wait_for_completion=False,
            webhook_id=webhook_id,
            user_id=user_id,
            event_id=event_id,
            timeout=config.PROVISIONING_TIMEOUT
        )
        
        if success:
            msg = f"Provisioning initiated for '{resource_name}' with custom image"
            
            # Send success notification
            if not notification.send_webhook_log(
                webhook_id=webhook_id,
                event_type=EVENT_START,
                success=True,
                payload_data=json.dumps(payload.model_dump()),
                status_code=200,
                response=msg,
                retry_count=0,
                metadata={"resourceName": resource_name, "userId": user_id, "eventId": event_id}
            ):
                logger.warning(f"Failed to send webhook log for server '{resource_name}'")
        
            logger.info(f"[{EVENT_START}] {msg} (Event ID: {event_id}).")
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