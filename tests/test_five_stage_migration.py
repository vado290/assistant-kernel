import importlib
import pytest

@pytest.mark.parametrize('module,entry', [('migrate_five_stage','migrate'),('migrate_five_stage','rollback'),
    ('migrate_p2_shared_baseline','migrate'),('adopt_p1_configuration','adopt')])
async def test_historical_migration_refuses_before_any_io(module,entry):
    with pytest.raises(PermissionError,match='retired'):
        await getattr(importlib.import_module('scripts.'+module),entry)()
