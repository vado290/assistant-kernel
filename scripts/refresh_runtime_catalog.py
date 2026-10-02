from assistant_kernel.config import KernelSettings
from assistant_kernel.services.runtime_catalog import RuntimeCatalogService


if __name__ == "__main__":
    print(RuntimeCatalogService(KernelSettings.from_environment()).refresh())
