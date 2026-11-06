### Removed — a restore check that nothing calls

`host_backup.restored_bootstrap` let a restored database into sealed activation. Its one caller went when that path was retired, and only a test assertion still reached it, so both are gone. A restored host stays guarded: `update_operation._fresh_root` refuses initialization while a restore marker exists.
