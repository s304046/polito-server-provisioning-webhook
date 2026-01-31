"""
Application configuration module.

This module handles all configuration settings for the webhook client,
including logging setup and Kubernetes configuration management.
Supports dynamic OS image mapping and multiple SSH keys.
"""
import logging
import os
from typing import Optional, Dict

from kubernetes import config as kube_config


class ConfigurationError(Exception):
    """Raised when there's an error in configuration."""
    pass


class HealthzFilter(logging.Filter):
    """Filter to exclude /healthz endpoint logs."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out log records containing '/healthz' requests."""
        return record.getMessage().find("/healthz") == -1


class LoggingConfig:
    """Manages logging configuration."""
    
    @staticmethod
    def setup_logger(name: str = "webhook_client") -> logging.Logger:
        """Set up a logger with appropriate formatting and level."""
        logger = logging.getLogger(name)
        log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_name, logging.INFO)
        logger.setLevel(log_level)
        
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger


class KubernetesConfig:
    """Manages Kubernetes configuration."""
    
    @staticmethod
    def load_config() -> None:
        """Load Kubernetes configuration (in-cluster or local kubeconfig)."""
        try:
            kube_config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config.")
        except kube_config.ConfigException:
            try:
                kube_config.load_kube_config()
                logger.info("Loaded local Kubernetes config (kubeconfig).")
            except kube_config.ConfigException:
                error_msg = "Could not load any Kubernetes configuration."
                logger.error(error_msg)
                raise ConfigurationError(error_msg)


class AppConfig:
    """Application configuration container."""
    
    def __init__(self):
        """Initialize configuration from environment variables."""
        # Kubernetes configuration
        self.k8s_namespace = os.environ.get("K8S_NAMESPACE", "default")
        self.bmh_api_group = os.environ.get("BMH_API_GROUP", "metal3.io")
        self.bmh_api_version = os.environ.get("BMH_API_VERSION", "v1alpha1")
        self.bmh_plural = os.environ.get("BMH_PLURAL", "baremetalhosts")
        
        # --- Image configuration (Dynamic OS Mapping) ---
        # Default fallback values
        base_img = os.environ.get("PROVISION_IMAGE", "default-provision-image-url")
        base_chk = os.environ.get("PROVISION_CHECKSUM", "default-provision-checksum-url")

        self.os_images: Dict[str, str] = {
            "ubuntu": os.environ.get("IMG_UBUNTU", base_img),
            "debian": os.environ.get("IMG_DEBIAN", base_img),
            "rocky": os.environ.get("IMG_ROCKY", base_img),
            "fedora": os.environ.get("IMG_FEDORA", base_img)
        }
        
        self.os_checksums: Dict[str, str] = {
            "ubuntu": os.environ.get("CHK_UBUNTU", base_chk),
            "debian": os.environ.get("CHK_DEBIAN", base_chk),
            "rocky": os.environ.get("CHK_ROCKY", base_chk),
            "fedora": os.environ.get("CHK_FEDORA", base_chk)
        }

        self.provision_image = self.os_images["ubuntu"]
        self.provision_checksum = self.os_checksums["ubuntu"]
        self.provision_checksum_type = os.environ.get("PROVISION_CHECKSUM_TYPE", "sha256")
        self.deprovision_image = os.environ.get("DEPROVISION_IMAGE", "")
        
        # Security configuration
        self.webhook_secret = os.environ.get("WEBHOOK_SECRET")
        
        # Server configuration
        self.port = int(os.environ.get("PORT", "8080"))
        self.provisioning_timeout = int(os.environ.get("PROVISIONING_TIMEOUT", "600"))
        
        # Notification configuration
        self.notification_endpoint = os.environ.get("NOTIFICATION_ENDPOINT")
        self.notification_timeout = int(os.environ.get("NOTIFICATION_TIMEOUT", "30"))
        
        # Webhook log configuration
        self.webhook_log_endpoint = os.environ.get("WEBHOOK_LOG_ENDPOINT")
        self.webhook_log_timeout = int(os.environ.get("WEBHOOK_LOG_TIMEOUT", "30"))
        
        # Logging configuration
        self.disable_healthz_logs = os.environ.get("DISABLE_HEALTHZ_LOGS", "true").lower() == "true"
        
        self._validate_config()
    
    def _validate_config(self) -> None:
        """Validate configuration values."""
        if not self.os_images["ubuntu"] or self.os_images["ubuntu"] == "default-provision-image-url":
            logger.warning("Default PROVISION_IMAGE (Ubuntu) not configured.")
        
        if not self.webhook_secret:
            logger.warning("WEBHOOK_SECRET not configured. Signature verification skipped.")


# Initialize configuration
logger = LoggingConfig.setup_logger("server_provisioning_webhook_client")
config = AppConfig()

if config.disable_healthz_logs:
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthzFilter())

KubernetesConfig.load_config()

# Export values for backward compatibility and easy access
K8S_NAMESPACE = config.k8s_namespace
BMH_API_GROUP = config.bmh_api_group
BMH_API_VERSION = config.bmh_api_version
BMH_PLURAL = config.bmh_plural

# Multi-OS Exports
OS_IMAGES = config.os_images
OS_CHECKSUMS = config.os_checksums

PROVISION_IMAGE = config.provision_image
PROVISION_CHECKSUM = config.provision_checksum
PROVISION_CHECKSUM_TYPE = config.provision_checksum_type
DEPROVISION_IMAGE = config.deprovision_image
WEBHOOK_SECRET = config.webhook_secret
PORT = config.port
DISABLE_HEALTHZ_LOGS = config.disable_healthz_logs
PROVISIONING_TIMEOUT = config.provisioning_timeout
NOTIFICATION_ENDPOINT = config.notification_endpoint
NOTIFICATION_TIMEOUT = config.notification_timeout
WEBHOOK_LOG_ENDPOINT = config.webhook_log_endpoint
WEBHOOK_LOG_TIMEOUT = config.webhook_log_timeout