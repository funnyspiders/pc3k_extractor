#!/usr/bin/env python
# -*- coding: utf-8 -*-


import sys
import zlib

from firebird.driver import connect
from pathlib import Path
from pathvalidate import sanitize_filename

BLOB_CHUNK_SIZE = 1024 * 1024  # 1MB


# Not just sanitize, but replace \x00 to avoid
# "embedded null byte" errors on write.
# Also replace "\" and "/" before sanitizing since later they will be
# undistinguishable from path separators.
def sanitize_filename2(filename, sanitize=True):
    if sanitize:
        return sanitize_filename(
            filename.replace(
                "\x00", "_").replace("\\", "-").replace("/", "-"))
    else:
        return filename


class PC3K_Folder(object):
    def __init__(
        self,
        id_,
        folder_name,
        parent_id,
        folder_id,
        parent_folder_id,
        folder_type,
        last_data_modify,
        export_date
    ):
        self.id_ = id_
        self.folder_name = folder_name
        self.parent_id = parent_id
        self.folder_id = folder_id
        self.parent_folder_id = parent_folder_id
        self.folder_type = folder_type
        self.last_data_modify = last_data_modify
        self.export_date = export_date


class PC3K_Data(object):
    def __init__(
        self,
        id_,
        mod_name,
        data_id,
        data_size,
        crc,
        t_create,
        t_update,
        data,
        folder_id,
        profile,
        kind,
        row_data_crc,
        row_prf_crc,
        damaged,
        datakey,
    ):
        self.id_ = id_
        self.mod_name = mod_name
        self.data_id = data_id
        self.data_size = data_size
        self.crc = crc
        self.t_create = t_create
        self.t_update = t_update
        self.data = data
        self.folder_id = folder_id
        self.profile = profile
        self.kind = kind
        self.row_data_crc = row_data_crc
        self.row_prf_crc = row_prf_crc
        self.damaged = damaged
        self.datakey = datakey

    def GetDataPath(self, db_conn, sanitize=True):
        return Path(
            self.GetDataPathRecursive(
                db_conn, Path(), self.folder_id, sanitize=sanitize),
            sanitize_filename2(self.mod_name, sanitize=sanitize)
        )

    def GetDataPathRecursive(self, db_conn, path, folder_id, sanitize=True):
        with db_conn.cursor() as cursor:
            query = f"SELECT * FROM FOLDERS WHERE ID = {folder_id}"
            cursor.execute(query)
            res = cursor.fetchone()
            folder = PC3K_Folder(*res)
            sanitized_name = sanitize_filename2(
                folder.folder_name, sanitize=sanitize)
            path = Path(sanitized_name, path)
            if folder.parent_id != 0:
                path = self.GetDataPathRecursive(
                    db_conn, path, folder.parent_id)
            return path


class PC3K_Extractor(object):
    def __init__(
        self,
        db_file="input/pc3k.gdb",
        db_user="SYSDBA",
        db_password="masterkey",
        db_charset="WIN1251",
        in_dir="input",
        out_dir="output",
    ):
        self.db_user = db_user
        self.db_password = db_password
        self.db_charset = db_charset
        self.db_file = Path(in_dir, db_file)
        self.out_dir = Path(out_dir)

        self.OpenDatabase()
        self.InitState()

    def InitState(self):
        self.out_files = {}
        self.record_ids_with_errors = set()

    def OpenDatabase(self):
        self.db_conn = connect(
            self.db_file,
            user=self.db_user,
            password=self.db_password,
            charset=self.db_charset
        )

    def WriteBlob(self, id_, record_count, out_file, blob):
        if out_file.parent.exists() and not out_file.parent.is_dir():
            new_out_file = out_file.parent.with_name(
                f"{out_file.parent.name}_") / out_file.name
            print(f"Error: parent dir already exists as a file for: "
                  f"{out_file} . Will instead write to "
                  f"{new_out_file}", file=sys.stderr)
            out_file = new_out_file

        out_file.parent.mkdir(parents=True, exist_ok=True)

        blob_zlibed = False
        blob_invalid_ex = None

        if hasattr(blob, "read"):
            raw = bytes(blob.read())
        elif isinstance(blob, (bytes, bytearray, memoryview)):
            raw = bytes(blob)
        else:
            raise TypeError(
                f"Unsupported blob type: {type(blob).__name__}; "
                "expected bytes-like data or a readable stream."
            )

        if len(raw) >= 6 and raw[4:6] == b"\x78\xda":
            blob_zlibed = True
            decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS)
            try:
                output = decompressor.decompress(raw[4:])
                output += decompressor.flush()
                if not decompressor.eof:
                    raise zlib.error("Incomplete zlib stream")
            except zlib.error as ex:
                # Still write a blob if it is invalid, raise an exception
                # after write.
                blob_invalid_ex = ex
                output = raw
        else:
            output = raw
        # Blob statuses:
        # Z/OK - zlibbed and correct
        # Z/ERR - zlibbed, but incorrect
        # R/OK - raw
        print(f"Write: {id_}/{record_count}: "
              f"{'Z' if blob_zlibed else 'R'}/"
              f"{'ERR' if blob_invalid_ex else 'OK'}: {out_file}")
        out_file.write_bytes(output)
        if blob_invalid_ex:
            raise ValueError(
                "Invalid or corrupted zlib blob") from blob_invalid_ex

    def process(self):
        self.InitState()

        self.out_dir.mkdir(parents=True, exist_ok=True)

        print("Processing records...\n"
              "Program parameters:\n"
              f"Input DB file: {self.db_file}\n"
              f"Output directory: {self.out_dir}\n"
              f"DB charset: {self.db_charset}\n"
              f"Blob chunk size: {BLOB_CHUNK_SIZE}\n")

        with self.db_conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM data")
            self.record_count = cursor.fetchall()[0][0]

            query = "SELECT * FROM DATA ORDER BY ID"
            cursor.execute(query)

            for row in cursor:
                record = PC3K_Data(*row)
                self.ProcessRecord(record)

            print("\n------------\nDone!")
            if self.record_ids_with_errors:
                print("========= Record IDs with errors:\n"
                      f"{self.record_ids_with_errors}", file=sys.stderr)

            print("========= Duplicates:"),
            for k, v in self.out_files.items():
                if len(v) > 1:
                    for i in v:
                        print(f"==> {i['record'].id_}:\n"
                              f"out_file:       {k}\n"
                              f"out_file_dirty: {i['out_file_dirty']}\n"
                              f"data_size:      {i['record'].data_size}\n"
                              f"t_create:       {i['record'].t_create}\n"
                              f"t_update:       {i['record'].t_update}\n")

    def CheckRecordToWrite(self, record, out_file, out_file_dirty):
        # Write output file for a record only if there are no
        # duplicates for the output file path or this record has
        # the most recent t_update of all duplicates.
        result = True
        item = {
            "record": record,
            "out_file_dirty": out_file_dirty
        }
        if out_file in self.out_files.keys():
            for i in self.out_files[out_file]:
                if record.t_update < i['record'].t_update:
                    result = False
            self.out_files[out_file].append(item)
        else:
            self.out_files[out_file] = [item,]
        return result

    def ProcessRecord(self, record):
        try:
            out_file = Path(
                self.out_dir, record.GetDataPath(self.db_conn))
            out_file_dirty = Path(self.out_dir, record.GetDataPath(
                self.db_conn, sanitize=False))
        except Exception as ex:
            print(f"Error GetDataPath: record ID: {record.id_}: ===> "
                  f"{ex}", file=sys.stderr)
            self.record_ids_with_errors.add(record.id_)
            return

        if self.CheckRecordToWrite(record, out_file, out_file_dirty):
            try:
                self.WriteBlob(
                    record.id_, self.record_count, out_file,
                    record.data)
            except Exception as ex:
                print(f"Error WriteBlob: record ID: {record.id_}: "
                      f"{out_file} ===> {ex}", file=sys.stderr)
                self.record_ids_with_errors.add(record.id_)
        else:
            print(
                f"===> Skipped duplicate ID: {record.id_}: {out_file}",
                file=sys.stderr)


if __name__ == "__main__":
    extractor = PC3K_Extractor()
    extractor.process()
