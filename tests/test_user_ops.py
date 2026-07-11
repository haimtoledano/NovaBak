import pytest
from services.user_ops import (
    _gen_temp_password,
    user_to_dict,
    create_user,
    delete_user,
    reset_mfa,
    change_password,
    update_role,
)
from models import User
import auth


def test_gen_temp_password():
    pwd = _gen_temp_password(16)
    assert len(pwd) == 16
    assert isinstance(pwd, str)


def test_create_user(db_session):
    user, temp_pwd = create_user(db_session, "testuser", "operator", "system")

    assert user.username == "testuser"
    assert user.role == "operator"
    assert user.is_mfa_enabled is False
    assert user.hashed_password is not None
    assert auth.verify_password(temp_pwd, user.hashed_password) is True

    # Retrieve from DB to ensure it was saved
    db_user = db_session.query(User).filter(User.username == "testuser").first()
    assert db_user is not None


def test_user_to_dict(db_session):
    user, _ = create_user(db_session, "dictuser", "admin", "system")
    user.email = "dict@test.com"
    db_session.commit()
    d = user_to_dict(user)

    assert d["username"] == "dictuser"
    assert d["role"] == "admin"
    assert d["email"] == "dict@test.com"
    assert d["is_mfa_enabled"] is False


def test_delete_user(db_session):
    user, _ = create_user(db_session, "deluser", "viewer", "system")

    target = delete_user(db_session, user.id, current_username="admin")
    assert target.username == "deluser"

    # Verify deleted
    db_user = db_session.query(User).filter(User.username == "deluser").first()
    assert db_user is None


def test_delete_user_self(db_session):
    user, _ = create_user(db_session, "selfuser", "admin", "system")

    # Trying to delete oneself should fail
    with pytest.raises(ValueError, match="Cannot delete your own account"):
        delete_user(db_session, user.id, current_username="selfuser")


def test_reset_mfa(db_session):
    user, _ = create_user(db_session, "mfauser", "admin", "system")
    user.is_mfa_enabled = True
    user.mfa_secret = "SECRET"
    db_session.commit()

    target = reset_mfa(db_session, user.id)
    assert target.is_mfa_enabled is False

    db_user = db_session.query(User).filter(User.id == user.id).first()
    assert db_user.is_mfa_enabled is False
    assert db_user.mfa_secret is None


def test_change_password(db_session):
    user, temp_pwd = create_user(db_session, "pwduser", "admin", "system")
    
    # Test failure due to complexity
    with pytest.raises(ValueError, match="uppercase letter"):
        change_password(db_session, user.username, temp_pwd, "newpassword123")
        
    # Test success
    target = change_password(db_session, user.username, temp_pwd, "NewPassword123!")
    
    db_user = db_session.query(User).filter(User.id == user.id).first()
    assert auth.verify_password("NewPassword123!", db_user.hashed_password) is True


def test_update_role(db_session):
    user, _ = create_user(db_session, "roleuser", "viewer", "system")

    target = update_role(db_session, user.id, "operator", current_username="admin")

    db_user = db_session.query(User).filter(User.id == user.id).first()
    assert db_user.role == "operator"
