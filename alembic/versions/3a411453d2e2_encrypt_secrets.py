"""encrypt_secrets

Revision ID: 3a411453d2e2
Revises: 29ecd925f9dd
Create Date: 2026-07-11 23:27:16.125260

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.ext.declarative import declarative_base
from security import SecretManager

# revision identifiers, used by Alembic.
revision: str = '3a411453d2e2'
down_revision: Union[str, Sequence[str], None] = '29ecd925f9dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Use a lightweight Base to query tables
Base = declarative_base()

class ESXiHost(Base):
    __tablename__ = "esxi_hosts"
    id = sa.Column(sa.Integer, primary_key=True, index=True)
    password = sa.Column(sa.String)

class Config(Base):
    __tablename__ = "config"
    id = sa.Column(sa.Integer, primary_key=True, index=True)
    smb_password = sa.Column(sa.String, default="")
    smtp_password = sa.Column(sa.String, default="")
    imap_password = sa.Column(sa.String, default="")
    s3_secret_key = sa.Column(sa.String, default="")

def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    # 1. Encrypt ESXiHost passwords
    for host in session.query(ESXiHost).all():
        if host.password and not host.password.startswith("enc:"):
            host.password = SecretManager.encrypt(host.password)
    
    # 2. Encrypt Config passwords
    config = session.query(Config).first()
    if config:
        if config.smb_password and not config.smb_password.startswith("enc:"):
            config.smb_password = SecretManager.encrypt(config.smb_password)
        if config.smtp_password and not config.smtp_password.startswith("enc:"):
            config.smtp_password = SecretManager.encrypt(config.smtp_password)
        if config.imap_password and not config.imap_password.startswith("enc:"):
            config.imap_password = SecretManager.encrypt(config.imap_password)
        if config.s3_secret_key and not config.s3_secret_key.startswith("enc:"):
            config.s3_secret_key = SecretManager.encrypt(config.s3_secret_key)
            
    session.commit()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    # 1. Decrypt ESXiHost passwords
    for host in session.query(ESXiHost).all():
        if host.password and host.password.startswith("enc:"):
            host.password = SecretManager.decrypt(host.password)
            
    # 2. Decrypt Config passwords
    config = session.query(Config).first()
    if config:
        if config.smb_password and config.smb_password.startswith("enc:"):
            config.smb_password = SecretManager.decrypt(config.smb_password)
        if config.smtp_password and config.smtp_password.startswith("enc:"):
            config.smtp_password = SecretManager.decrypt(config.smtp_password)
        if config.imap_password and config.imap_password.startswith("enc:"):
            config.imap_password = SecretManager.decrypt(config.imap_password)
        if config.s3_secret_key and config.s3_secret_key.startswith("enc:"):
            config.s3_secret_key = SecretManager.decrypt(config.s3_secret_key)

    session.commit()
