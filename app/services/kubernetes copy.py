"""
Kubernetes service for managing BareMetalHost resources.

This module provides functionality to interact with Kubernetes API
to manage BareMetalHost custom resources and related secrets for server provisioning.
"""
import asyncio
import base64
import threading
import time
from typing import Optional

import yaml
from kubernetes import client, watch
from kubernetes.client.rest import ApiException

from .. import config

logger = config.logger

# Cloud-config template for user data
CLOUD_CONFIG_TEMPLATE = {
    "ssh_pwauth": True,
    "groups": ["admingroup", "cloud-users"],
    "users": [
        {
            "name": "restart.admin",
            "groups": "admingroup",
            "lock_passwd": False,
            "passwd": "$6$/O/rvHuhqfc00hDw$3X4ILugPTXw9JTtgWNh16oeFqLcsMOaPwzk7TBxtwm5QXa2vALMC2W7/JToC99ngxpKla80QpVAEs3jA8I0rk0",
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
        },
        {
            "name": "prognose",  # New user for external access
            "groups": "cloud-users",
            "lock_passwd": True,  # Lock password, access only via SSH key
            "sudo": "ALL=(ALL) NOPASSWD:ALL",  # No sudo privileges for external user
            "ssh_authorized_keys": []  # Will be populated dynamically
        }
    ]
}


class KubernetesError(Exception):
    """Custom exception for Kubernetes operations."""
    pass


class UserDataSecretManager:
    """Manages user data secrets for BareMetalHost resources."""
    
    def __init__(self, api_client: Optional[client.CoreV1Api] = None):
        self.api = api_client or client.CoreV1Api()
    
    def _generate_cloud_config(self, ssh_key: str) -> str:
        """
        Generate cloud-config YAML with the provided SSH key.
        
        Args:
            ssh_key: SSH public key to include in the cloud-config
            
        Returns:
            Cloud-config as YAML string
        """
        cloud_config = CLOUD_CONFIG_TEMPLATE.copy()
        # Ensure users list is deep copied if further modifications are needed that could affect the template
        cloud_config["users"] = [user.copy() for user in CLOUD_CONFIG_TEMPLATE["users"]]

        # Find the "prognose" and add the ssh_key
        for user in cloud_config["users"]:
            if user["name"] == "prognose":
                user["ssh_authorized_keys"] = [ssh_key]
                break
        
        return "#cloud-config\n" + yaml.dump(cloud_config, default_flow_style=False)
    
    def _encode_cloud_config(self, cloud_config: str) -> str:
        """
        Encode cloud-config to base64.
        
        Args:
            cloud_config: Cloud-config as string
            
        Returns:
            Base64 encoded cloud-config
        """
        return base64.b64encode(cloud_config.encode('utf-8')).decode('utf-8')
    
    def _create_secret_object(self, secret_name: str, cloud_config_b64: str) -> client.V1Secret:
        """
        Create a Kubernetes Secret object.
        
        Args:
            secret_name: Name of the secret
            cloud_config_b64: Base64 encoded cloud-config
            
        Returns:
            V1Secret object
        """
        return client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=config.K8S_NAMESPACE
            ),
            type="Opaque",
            data={"userData": cloud_config_b64}
        )
    
    def create_or_update(self, bmh_name: str, ssh_key: str) -> bool:
        """
        Create or update a user data secret for the BareMetalHost.
        
        Args:
            bmh_name: Name of the BareMetalHost
            ssh_key: SSH public key to include
            
        Returns:
            True if successful, False otherwise
        """
        secret_name = f"{bmh_name}-userdata"
        
        try:
            # Generate and encode cloud-config
            cloud_config = self._generate_cloud_config(ssh_key)
            cloud_config_b64 = self._encode_cloud_config(cloud_config)
            
            # Create secret object
            secret = self._create_secret_object(secret_name, cloud_config_b64)
            
            # Try to create the secret
            try:
                self.api.create_namespaced_secret(namespace=config.K8S_NAMESPACE, body=secret)
                logger.info(f"Created secret '{secret_name}' in namespace '{config.K8S_NAMESPACE}'.")
                return True
                
            except ApiException as e:
                if e.status == 409:  # Secret already exists, update it
                    self.api.patch_namespaced_secret(
                        name=secret_name, 
                        namespace=config.K8S_NAMESPACE, 
                        body=secret
                    )
                    logger.info(f"Updated existing secret '{secret_name}' in namespace '{config.K8S_NAMESPACE}'.")
                    return True
                else:
                    logger.error(
                        f"Error creating secret '{secret_name}': {e.reason} "
                        f"(Status: {e.status}). Body: {e.body}"
                    )
                    return False
                    
        except Exception as e:
            logger.error(f"Unexpected error while managing secret '{secret_name}': {str(e)}")
            return False


class BareMetalHostManager:
    """Manages BareMetalHost custom resources."""
    
    def __init__(self, api_client: Optional[client.CustomObjectsApi] = None):
        self.api = api_client or client.CustomObjectsApi()
        self.secret_manager = UserDataSecretManager()
    
    def _create_provision_patch(
        self, 
        image_url: str, 
        bmh_name: str,
        checksum: Optional[str] = None, 
        checksum_type: Optional[str] = None
    ) -> dict:
        """
        Create a patch for provisioning a BareMetalHost.
        
        Args:
            image_url: URL of the image to provision
            checksum: Image checksum
            checksum_type: Type of checksum (e.g., 'sha256')
            
        Returns:
            Patch dictionary for provisioning
        """
        return {
            "spec": {
                "image": {
                    "url": image_url,
                    "checksum": checksum,
                    "checksumType": checksum_type
                },
                "userData": {
                    "name": f"{bmh_name}-userdata",
                    "namespace": config.K8S_NAMESPACE
                }
            }
        }
    
    def _create_deprovision_patch(self) -> dict:
        """
        Create a patch for deprovisioning a BareMetalHost.
        
        Returns:
            Patch dictionary for deprovisioning
        """
        return {
            "spec": {
                "image": None,
                "userData": None
            }
        }
    
    def _apply_patch(self, bmh_name: str, patch: dict, operation: str) -> bool:
        """
        Apply a patch to a BareMetalHost resource.
        
        Args:
            bmh_name: Name of the BareMetalHost
            patch: Patch to apply
            operation: Description of the operation (for logging)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(
                f"Attempting to {operation} BareMetalHost '{bmh_name}' "
                f"in namespace '{config.K8S_NAMESPACE}'."
            )
            
            response = self.api.patch_namespaced_custom_object(
                group=config.BMH_API_GROUP,
                version=config.BMH_API_VERSION,
                namespace=config.K8S_NAMESPACE,
                plural=config.BMH_PLURAL,
                name=bmh_name,
                body=patch
            )

            logger.debug(
                f"Patch response for BareMetalHost '{bmh_name}': {response}"
            )
            
            logger.info(f"Successfully {operation}ed BareMetalHost '{bmh_name}'.")
            return True
            
        except ApiException as e:
            logger.error(
                f"Error {operation}ing BareMetalHost '{bmh_name}': {e.reason} "
                f"(Status: {e.status}). Body: {e.body}"
            )
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error while {operation}ing BareMetalHost '{bmh_name}': {str(e)}")
            return False
    
    def provision(
        self, 
        bmh_name: str, 
        image_url: str, 
        ssh_key: Optional[str] = None,
        checksum: Optional[str] = None, 
        checksum_type: Optional[str] = None,
        wait_for_completion: bool = False,
        webhook_id: Optional[str] = None,
        user_id: Optional[str] = None,
        event_id: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> bool:
        """
        Provision a BareMetalHost with the specified image.
        
        Args:
            bmh_name: Name of the BareMetalHost
            image_url: URL of the image to provision
            ssh_key: SSH public key for user access
            checksum: Image checksum
            checksum_type: Type of checksum
            wait_for_completion: Not used, kept for API compatibility
            webhook_id: Webhook identifier for asynchronous notifications
            user_id: User identifier for asynchronous notifications
            event_id: Event identifier for asynchronous notifications
            timeout: Maximum time to wait in seconds (for async monitoring)
            
        Returns:
            True if provisioning initiated successfully, False otherwise
        """
        # Create or update user data secret if SSH key is provided
        if ssh_key:
            if not self.secret_manager.create_or_update(bmh_name, ssh_key):
                logger.error(f"Failed to create userdata secret for BareMetalHost '{bmh_name}'. Aborting provision.")
                return False
        
        # Create and apply provision patch
        patch = self._create_provision_patch(image_url, bmh_name, checksum, checksum_type)
        success = self._apply_patch(bmh_name, patch, "provision")
        
        # If provisioning was successful and we have webhook parameters, start async monitoring
        if success and webhook_id and user_id:
            _provisioning_monitor.start_monitoring_async(
                bmh_name=bmh_name,
                webhook_id=webhook_id,
                user_id=user_id,
                event_id=event_id,
                timeout=timeout
            )
            logger.info(f"Started asynchronous monitoring for BareMetalHost '{bmh_name}' provisioning")
        
        return success
    
    def deprovision(self, bmh_name: str) -> bool:
        """
        Deprovision a BareMetalHost by clearing its image configuration.
        
        Args:
            bmh_name: Name of the BareMetalHost
            
        Returns:
            True if successful, False otherwise
        """
        patch = self._create_deprovision_patch()
        return self._apply_patch(bmh_name, patch, "deprovision")
    
    def wait_for_provisioning(
        self, 
        bmh_name: str, 
        timeout: int = None
    ) -> bool:
        """
        Wait for BareMetalHost provisioning to complete using Kubernetes watch API.
        
        Args:
            bmh_name: Name of the BareMetalHost
            timeout: Maximum time to wait in seconds (uses config if None)
            
        Returns:
            True if provisioning completed successfully, False if timeout or error
        """
        timeout = timeout or config.PROVISIONING_TIMEOUT
        
        logger.info(f"Waiting for BareMetalHost '{bmh_name}' provisioning to complete using watch API (timeout: {timeout}s)")
        
        # Create a watch object for monitoring BareMetalHost resources
        w = watch.Watch()
        
        try:
            # First, get the current state to check if it's already provisioned
            try:
                current_bmh = self.api.get_namespaced_custom_object(
                    group=config.BMH_API_GROUP,
                    version=config.BMH_API_VERSION,
                    namespace=config.K8S_NAMESPACE,
                    plural=config.BMH_PLURAL,
                    name=bmh_name
                )
                
                current_status = current_bmh.get('status', {})
                current_provisioning = current_status.get('provisioning', {})
                current_state = current_provisioning.get('state', '')
                
                logger.debug(f"BareMetalHost '{bmh_name}' initial state: '{current_state}'")
                
                # Check if already in final state
                if current_state == 'provisioned':
                    logger.info(f"BareMetalHost '{bmh_name}' is already provisioned")
                    return True
                elif current_state in ['error', 'failed']:
                    logger.error(f"BareMetalHost '{bmh_name}' is already in failed state: '{current_state}'")
                    return False
                    
            except ApiException as e:
                logger.error(f"Error getting initial state of BareMetalHost '{bmh_name}': {e.reason}")
                return False
            
            # Watch for changes to the specific BareMetalHost
            for event in w.stream(
                self.api.list_namespaced_custom_object,
                group=config.BMH_API_GROUP,
                version=config.BMH_API_VERSION,
                namespace=config.K8S_NAMESPACE,
                plural=config.BMH_PLURAL,
                field_selector=f"metadata.name={bmh_name}",
                timeout_seconds=timeout
            ):
                event_type = event['type']  # ADDED, MODIFIED, DELETED
                bmh_object = event['object']
                
                # Only process MODIFIED events (status changes)
                if event_type in ['MODIFIED', 'ADDED']:
                    status = bmh_object.get('status', {})
                    provisioning = status.get('provisioning', {})
                    state = provisioning.get('state', '')
                    
                    logger.debug(f"BareMetalHost '{bmh_name}' watch event: {event_type}, state: '{state}'")
                    
                    if state == 'provisioned':
                        logger.info(f"BareMetalHost '{bmh_name}' provisioning completed successfully")
                        w.stop()
                        return True
                    elif state in ['error', 'failed']:
                        logger.error(f"BareMetalHost '{bmh_name}' provisioning failed with state: '{state}'")
                        w.stop()
                        return False
                    elif state in ['preparing', 'provisioning', 'inspecting']:
                        logger.info(f"BareMetalHost '{bmh_name}' is in state: '{state}' - continuing to watch...")
                
                elif event_type == 'DELETED':
                    logger.error(f"BareMetalHost '{bmh_name}' was deleted during provisioning")
                    w.stop()
                    return False
            
            # If we reach here, the watch timed out
            logger.error(f"Timeout waiting for BareMetalHost '{bmh_name}' provisioning (waited {timeout}s)")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error while watching BareMetalHost '{bmh_name}': {str(e)}")
            return False
        finally:
            # Ensure the watch is stopped
            w.stop()


class ProvisioningMonitor:
    """Handles asynchronous monitoring of BareMetalHost provisioning and notifications."""
    
    def __init__(self, bmh_manager: Optional[BareMetalHostManager] = None):
        self.bmh_manager = bmh_manager or _bmh_manager
    
    def start_monitoring_async(
        self,
        bmh_name: str,
        webhook_id: str,
        user_id: str,
        event_id: Optional[str] = None,
        timeout: Optional[int] = None
    ) -> None:
        """
        Start asynchronous monitoring of BareMetalHost provisioning.
        
        This method launches a separate thread to monitor provisioning completion
        and send notifications without blocking the main request thread.
        
        Args:
            bmh_name: Name of the BareMetalHost to monitor
            webhook_id: Webhook identifier for notifications
            user_id: User identifier for notifications
            event_id: Event identifier for notifications
            timeout: Maximum time to wait in seconds (uses config if None)
        """
        monitoring_thread = threading.Thread(
            target=self._monitor_provisioning_completion,
            args=(bmh_name, webhook_id, user_id, event_id, timeout),
            daemon=True,
            name=f"ProvisioningMonitor-{bmh_name}"
        )
        monitoring_thread.start()
        logger.info(f"Started asynchronous provisioning monitoring for BareMetalHost '{bmh_name}'")
    
    def _monitor_provisioning_completion(
        self,
        bmh_name: str,
        webhook_id: str,
        user_id: str,
        event_id: Optional[str],
        timeout: Optional[int]
    ) -> None:
        """
        Internal method to monitor provisioning and send notifications.
        
        This method runs in a separate thread and handles:
        1. Waiting for provisioning completion
        2. Sending success/failure notifications
        3. Error handling and logging
        """
        try:
            logger.info(f"Starting provisioning monitor for BareMetalHost '{bmh_name}' in background thread")
            
            # Wait for provisioning to complete
            success = self.bmh_manager.wait_for_provisioning(bmh_name, timeout)
            
            # Send notification about the result
            error_message = None if success else "Provisioning timeout or failed"
            self._send_notification(
                webhook_id=webhook_id,
                user_id=user_id,
                resource_name=bmh_name,
                success=success,
                error_message=error_message,
                event_id=event_id
            )
            
            if success:
                logger.info(f"BareMetalHost '{bmh_name}' provisioning completed successfully. Notification sent.")
            else:
                logger.error(f"BareMetalHost '{bmh_name}' provisioning failed or timed out. Notification sent.")
                
        except Exception as e:
            logger.error(f"Error in provisioning monitor for BareMetalHost '{bmh_name}': {str(e)}")
            # Send failure notification on unexpected error
            self._send_notification(
                webhook_id=webhook_id,
                user_id=user_id,
                resource_name=bmh_name,
                success=False,
                error_message=f"Monitoring error: {str(e)}",
                event_id=event_id
            )
    
    def _send_notification(
        self,
        webhook_id: str,
        user_id: str,
        resource_name: str,
        success: bool,
        error_message: Optional[str] = None,
        event_id: Optional[str] = None
    ) -> None:
        """
        Send notification about provisioning completion and log webhook event.
        
        Args:
            webhook_id: Webhook identifier
            user_id: User identifier
            resource_name: Name of the BareMetalHost resource
            success: Whether provisioning was successful
            error_message: Error message if provisioning failed
            event_id: Event identifier
        """
        try:
            # Import here to avoid circular imports
            from .notification import send_provisioning_notification, send_webhook_log
            
            # Send the provisioning notification
            notification_sent = send_provisioning_notification(
                webhook_id=webhook_id,
                user_id=user_id,
                resource_name=resource_name,
                success=success,
                error_message=error_message,
                event_id=event_id
            )
            
            # Determine event type and response details for webhook log
            event_type = "EVENT_START"  # Provisioning monitoring is for EVENT_START
            status_code = 200 if success else 500
            response_message = "Provisioning completed successfully" if success else f"Provisioning failed: {error_message or 'Unknown error'}"
            
            # Send webhook log
            webhook_log_sent = send_webhook_log(
                webhook_id=webhook_id,
                event_type=event_type,
                success=success,
                payload_data=f"Provisioning monitoring for resource '{resource_name}'",
                status_code=status_code,
                response=response_message,
                retry_count=0,
                metadata={
                    "resourceName": resource_name,
                    "userId": user_id,
                    "eventId": event_id,
                    "errorMessage": error_message
                }
            )
            
            if not notification_sent:
                logger.warning(f"Failed to send notification for resource '{resource_name}'")
            else:
                logger.debug(f"Successfully sent notification for resource '{resource_name}' (success: {success})")
                
            if not webhook_log_sent:
                logger.warning(f"Failed to send webhook log for resource '{resource_name}'")
            else:
                logger.debug(f"Successfully sent webhook log for resource '{resource_name}' (success: {success})")
                
        except Exception as e:
            logger.error(f"Error sending notification/webhook log for resource '{resource_name}': {str(e)}")


# Singleton instances for backward compatibility
_bmh_manager = BareMetalHostManager()
_provisioning_monitor = ProvisioningMonitor(_bmh_manager)


def patch_baremetalhost(
    bmh_name: str, 
    image_url: Optional[str] = None, 
    ssh_key: Optional[str] = None, 
    checksum: Optional[str] = None, 
    checksum_type: Optional[str] = None,
    wait_for_completion: bool = False,
    webhook_id: Optional[str] = None,
    user_id: Optional[str] = None,
    event_id: Optional[str] = None,
    timeout: Optional[int] = None
) -> bool:
    """
    Patch a BareMetalHost for provisioning or deprovisioning.
    
    This function maintains backward compatibility with the existing API.
    
    Args:
        bmh_name: Name of the BareMetalHost
        image_url: URL of the image to provision (None for deprovisioning)
        ssh_key: SSH public key for user access
        checksum: Image checksum
        checksum_type: Type of checksum
        wait_for_completion: Whether to wait for provisioning to complete
        webhook_id: Webhook identifier for asynchronous notifications
        user_id: User identifier for asynchronous notifications
        event_id: Event identifier for asynchronous notifications
        timeout: Maximum time to wait in seconds (for async monitoring)
        
    Returns:
        True if successful, False otherwise
    """
    if image_url:
        return _bmh_manager.provision(
            bmh_name, image_url, ssh_key, checksum, checksum_type, 
            wait_for_completion, webhook_id, user_id, event_id, timeout
        )
    else:
        return _bmh_manager.deprovision(bmh_name)


# Legacy function alias for backward compatibility
create_userdata_secret = lambda bmh_name, ssh_key: _bmh_manager.secret_manager.create_or_update(bmh_name, ssh_key)
