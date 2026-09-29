-- Collapse the four-role model to three: admin, manager, user.
--
--   admin    full access, including who may do what
--   manager  full access to settings and activity, but cannot change access rights
--   user     uses the app; no settings, no member management
--
-- The separation that matters is between *using* the product, *configuring* it,
-- and *deciding who may do either*. The old owner/admin split did not express
-- that: both could change roles, so there was no level that could manage
-- settings without also being able to promote itself.

-- Drop the constraints first: the remap below writes values the old CHECKs
-- would reject.
ALTER TABLE memberships DROP CONSTRAINT IF EXISTS memberships_role_check;
ALTER TABLE invites     DROP CONSTRAINT IF EXISTS invites_role_check;

--   owner  -> admin    (kept full control, including access rights)
--   admin  -> manager  (settings yes, access rights no)
--   member -> user
--   viewer -> user
UPDATE memberships SET role = CASE role
    WHEN 'owner'  THEN 'admin'
    WHEN 'admin'  THEN 'manager'
    WHEN 'member' THEN 'user'
    WHEN 'viewer' THEN 'user'
    ELSE role
END;

UPDATE invites SET role = CASE role
    WHEN 'admin'  THEN 'manager'
    WHEN 'member' THEN 'user'
    WHEN 'viewer' THEN 'user'
    ELSE role
END
WHERE accepted_at IS NULL;

ALTER TABLE memberships
    ADD CONSTRAINT memberships_role_check
    CHECK (role IN ('admin', 'manager', 'user'));

-- Every role is invitable now. There is no longer a role that cannot be granted
-- by invitation, because 'admin' is a deliberate choice rather than an
-- ownership transfer.
ALTER TABLE invites
    ADD CONSTRAINT invites_role_check
    CHECK (role IN ('admin', 'manager', 'user'));

-- An organisation with no admin cannot be administered: nobody could restore
-- access, change settings, or invite anyone. Guaranteed in application code
-- (orgs.py refuses to demote or remove the last admin); this is the net
-- underneath it.
CREATE OR REPLACE FUNCTION assert_org_keeps_an_admin() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM memberships
        WHERE org_id = COALESCE(OLD.org_id, NEW.org_id) AND role = 'admin'
    ) THEN
        RAISE EXCEPTION 'an organisation must keep at least one admin';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER memberships_keep_admin
    AFTER UPDATE OR DELETE ON memberships
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_org_keeps_an_admin();
