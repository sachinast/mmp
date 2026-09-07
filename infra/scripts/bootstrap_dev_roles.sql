-- Give the application roles a LOGIN and a development password.
--
-- Separate from the migration on purpose. Migrations run in every environment,
-- and a password in a migration is a password in version control. In staging
-- and production these roles are given credentials out of band from KMS; this
-- file exists so that local development and CI can connect *as the application
-- roles* and therefore actually exercise RLS, rather than testing as the owner
-- and discovering in production that the policies were never enforced.

ALTER ROLE mmp_api      WITH LOGIN PASSWORD 'dev_only_api';
ALTER ROLE mmp_tracker  WITH LOGIN PASSWORD 'dev_only_tracker';
ALTER ROLE mmp_worker   WITH LOGIN PASSWORD 'dev_only_worker';
ALTER ROLE mmp_readonly WITH LOGIN PASSWORD 'dev_only_readonly';
