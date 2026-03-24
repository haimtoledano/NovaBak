import os
import shutil
from abc import ABC, abstractmethod
from logger_util import log_info, log_error, log_warn

class StorageProvider(ABC):
    @abstractmethod
    def exists(self, path): pass
    
    @abstractmethod
    def makedirs(self, path): pass
    
    @abstractmethod
    def open_write(self, path): pass
    
    @abstractmethod
    def open_read(self, path): pass
    
    @abstractmethod
    def list_dirs(self, path): pass
    
    @abstractmethod
    def list_files(self, path, extension=None): pass
    
    @abstractmethod
    def delete_dir(self, path): pass
    
    @abstractmethod
    def get_size(self, path): pass

    @abstractmethod
    def get_base_path(self): pass

class LocalStorageProvider(StorageProvider):
    def __init__(self, base_path):
        self.base_path = base_path

    def get_base_path(self):
        return self.base_path

    def _full_path(self, path):
        if os.path.isabs(path): return path
        return os.path.join(self.base_path, path)

    def exists(self, path):
        return os.path.exists(self._full_path(path))

    def makedirs(self, path):
        os.makedirs(self._full_path(path), exist_ok=True)

    def open_write(self, path):
        full_p = self._full_path(path)
        os.makedirs(os.path.dirname(full_p), exist_ok=True)
        return open(full_p, 'wb')

    def open_read(self, path):
        return open(self._full_path(path), 'rb')

    def list_dirs(self, path):
        full_p = self._full_path(path)
        if not os.path.exists(full_p): return []
        return [d for d in os.listdir(full_p) if os.path.isdir(os.path.join(full_p, d))]

    def list_files(self, path, extension=None):
        full_p = self._full_path(path)
        if not os.path.exists(full_p): return []
        files = [f for f in os.listdir(full_p) if os.path.isfile(os.path.join(full_p, f))]
        if extension:
            files = [f for f in files if f.endswith(extension)]
        return files

    def delete_dir(self, path):
        full_p = self._full_path(path)
        if os.path.exists(full_p):
            shutil.rmtree(full_p, ignore_errors=True)

    def get_size(self, path):
        full_p = self._full_path(path)
        if os.path.isfile(full_p):
            return os.path.getsize(full_p)
        total = 0
        for root, dirs, files in os.walk(full_p):
            for f in files:
                total += os.path.getsize(os.path.join(root, f))
        return total

class S3StorageProvider(StorageProvider):
    def __init__(self, endpoint, access_key, secret_key, bucket, region="us-east-1"):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.client(
            's3',
            endpoint_url=endpoint if endpoint else None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region
        )

    def get_base_path(self):
        return f"s3://{self.bucket}/"

    def exists(self, path):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=path)
            return True
        except:
            return False

    def makedirs(self, path):
        # S3 does not have directories, they are just prefixes
        pass

    def open_write(self, path):
        # For S3, we use a custom writer that uploads on close
        # Given the large size of VMDKs, we should use Multipart Upload.
        return S3MultipartWriter(self.s3, self.bucket, path)

    def open_read(self, path):
        # We can return the body of the object as a stream
        res = self.s3.get_object(Bucket=self.bucket, Key=path)
        return res['Body']

    def list_dirs(self, path):
        if path and not path.endswith('/'): path += '/'
        prefix = path
        result = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, Delimiter='/')
        dirs = []
        if 'CommonPrefixes' in result:
            for cp in result['CommonPrefixes']:
                # Extract the directory name from the prefix
                d = cp['Prefix'][len(prefix):].rstrip('/')
                if d: dirs.append(d)
        return dirs

    def list_files(self, path, extension=None):
        if path and not path.endswith('/'): path += '/'
        prefix = path
        result = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, Delimiter='/')
        files = []
        if 'Contents' in result:
            for obj in result['Contents']:
                f = obj['Key'][len(prefix):]
                if f and '/' not in f:
                    if not extension or f.endswith(extension):
                        files.append(f)
        return files

    def delete_dir(self, path):
        if path and not path.endswith('/'): path += '/'
        # Delete all objects with the prefix
        paginator = self.s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=path):
            if 'Contents' in page:
                delete_keys = {'Objects': [{'Key': obj['Key']} for obj in page['Contents']]}
                self.s3.delete_objects(Bucket=self.bucket, Delete=delete_keys)

    def get_size(self, path):
        total = 0
        paginator = self.s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=path):
            if 'Contents' in page:
                for obj in page['Contents']:
                    total += obj['Size']
        return total

class S3MultipartWriter:
    """ A basic mock-file object that performs S3 Multipart Upload. """
    def __init__(self, s3_client, bucket, key):
        self.s3 = s3_client
        self.bucket = bucket
        self.key = key
        self.upload_id = None
        self.parts = []
        self.buffer = b''
        self.part_number = 1
        # Minimum part size for S3 is 5MB
        self.min_part_size = 5 * 1024 * 1024 

        res = self.s3.create_multipart_upload(Bucket=self.bucket, Key=self.key)
        self.upload_id = res['UploadId']

    def write(self, data):
        self.buffer += data
        if len(self.buffer) >= self.min_part_size:
            self._upload_part()

    def _upload_part(self):
        if not self.buffer: return
        res = self.s3.upload_part(
            Bucket=self.bucket, Key=self.key,
            PartNumber=self.part_number, UploadId=self.upload_id,
            Body=self.buffer
        )
        self.parts.append({'ETag': res['ETag'], 'PartNumber': self.part_number})
        self.part_number += 1
        self.buffer = b''

    def close(self):
        if self.buffer or not self.parts:
            self._upload_part()
        self.s3.complete_multipart_upload(
            Bucket=self.bucket, Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={'Parts': self.parts}
        )

    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            if self.upload_id:
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)
        else:
            self.close()

    def seek(self, offset, whence=0):
        # Seek is NOT supported for S3 multipart upload. 
        # backup_engine.py needs to handle this (fallback to full write).
        pass

    def truncate(self, size):
        pass

def get_storage(config):
    if config.storage_type == "S3":
        return S3StorageProvider(
            config.s3_endpoint,
            config.s3_access_key,
            config.s3_secret_key,
            config.s3_bucket,
            config.s3_region
        )
    elif config.storage_type == "NFS":
        return LocalStorageProvider(config.nfs_path)
    else: # Default to SMB
        return LocalStorageProvider(config.smb_unc_path)
