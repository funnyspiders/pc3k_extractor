#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import logging
import sys
import zlib

from firebird.driver import connect
from pathlib import Path
from pathvalidate import sanitize_filename

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DB_CHARSET = "WIN1251"
DEFAULT_DB_USER = "SYSDBA"
DEFAULT_DB_PASSWORD = "masterkey"
DEFAULT_DB_FILE = "pc3k.gdb"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_LOG_LEVEL = "INFO"


class LoggingMaxLevelFilter(logging.Filter):
    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        return record.levelno <= self.max_level


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
    def __init__(self, args):
        self.log_level = (
            args.log_level if args.log_level else DEFAULT_LOG_LEVEL)
        self.InitLogging()

        self.db_file = Path(
            args.db_file if args.db_file else DEFAULT_DB_FILE)
        self.db_user = (
            args.db_user if args.db_user else DEFAULT_DB_USER)
        self.db_password = (
            args.db_password if args.db_password else DEFAULT_DB_PASSWORD)
        self.db_charset = (
            args.db_charset if args.db_charset else DEFAULT_DB_CHARSET)
        self.output_dir = Path(
            args.output_dir if args.output_dir else DEFAULT_OUTPUT_DIR)

        self.OpenDatabase()
        self.InitState()

    def InitLogging(self):
        numeric_level = getattr(logging, self.log_level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % self.log_level)
        else:
            self.logger = logging.getLogger('Extractor')
            self.logger.setLevel(numeric_level)
            formatter = logging.Formatter(
                '%(asctime)s: %(levelname)s: %(message)s')

            stdout_handler = logging.StreamHandler(sys.stdout)
            stdout_handler.setLevel(logging.DEBUG)
            stdout_handler.addFilter(LoggingMaxLevelFilter(logging.WARNING))
            stdout_handler.setFormatter(formatter)

            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setLevel(logging.ERROR)
            stderr_handler.setFormatter(formatter)

            self.logger.addHandler(stdout_handler)
            self.logger.addHandler(stderr_handler)

    def exit(self, code):
        self.logger.info("Exiting...")
        sys.exit(code)

    def InitState(self):
        self.out_files = {}
        self.record_ids_with_errors = set()

    def OpenDatabase(self):
        try:
            self.db_conn = connect(
                self.db_file,
                user=self.db_user,
                password=self.db_password,
                charset=self.db_charset
            )
        except Exception as ex:
            self.logger.error(f"Can't open the database: {ex}")
            self.exit(1)

    def WriteBlob(self, id_, record_count, out_file, blob):
        if out_file.parent.exists() and not out_file.parent.is_dir():
            new_out_file = out_file.parent.with_name(
                f"{out_file.parent.name}_") / out_file.name
            self.logger.error(
                f"Parent dir already exists as a file for: {out_file} . "
                f"Will instead write to {new_out_file}", file=sys.stderr)
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

        # Not sure why the header is at this offset and not at 0,
        # but it is what it is...
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
        self.logger.info(
            f"Write: {id_}/{record_count}: "
            f"{'Z' if blob_zlibed else 'R'}/"
            f"{'ERR' if blob_invalid_ex else 'OK'}: {out_file}")
        out_file.write_bytes(output)
        if blob_invalid_ex:
            raise ValueError(
                "Invalid or corrupted zlib blob") from blob_invalid_ex

    def process(self):
        self.InitState()

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(
            "Processing records...\n"
            "Program parameters:\n"
            f"Input DB file: {self.db_file}\n"
            f"Output directory: {self.output_dir}\n"
            f"DB charset: {self.db_charset}")

        with self.db_conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM data")
            self.record_count = cursor.fetchall()[0][0]

            query = "SELECT * FROM DATA ORDER BY ID"
            cursor.execute(query)

            for row in cursor:
                record = PC3K_Data(*row)
                self.ProcessRecord(record)

            self.logger.info("All done!")
            if self.record_ids_with_errors:
                self.logger.debug(
                    "Record IDs with errors:\n{self.record_ids_with_errors}")

            self.logger.debug("Duplicates:")
            for k, v in self.out_files.items():
                if len(v) > 1:
                    for i in v:
                        self.logger.debug(
                            f"==> {i['record'].id_}:\n"
                            f"out_file:       {k}\n"
                            f"out_file_dirty: {i['out_file_dirty']}\n"
                            f"data_size:      {i['record'].data_size}\n"
                            f"t_create:       {i['record'].t_create}\n"
                            f"t_update:       {i['record'].t_update}")

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
                self.output_dir, record.GetDataPath(self.db_conn))
            out_file_dirty = Path(self.output_dir, record.GetDataPath(
                self.db_conn, sanitize=False))
        except Exception as ex:
            self.logger.error(
                f"GetDataPath: record ID: {record.id_}: {ex}")
            self.record_ids_with_errors.add(record.id_)
            return

        if self.CheckRecordToWrite(record, out_file, out_file_dirty):
            try:
                self.WriteBlob(
                    record.id_, self.record_count, out_file,
                    record.data)
            except Exception as ex:
                self.logger.error(
                    f"WriteBlob: record ID: {record.id_}: {out_file}: {ex}")
                self.record_ids_with_errors.add(record.id_)
        else:
            self.logger.warning(
                f"Skipped duplicate ID: {record.id_}: {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-d", "--db_file",
        help=f"Input database file, default: {DEFAULT_DB_FILE}")
    parser.add_argument(
        "-u", "--db_user",
        help=f"Database user, default: {DEFAULT_DB_USER}")
    parser.add_argument(
        "-p", "--db_password",
        help=f"Database password, default: {DEFAULT_DB_PASSWORD}")
    parser.add_argument(
        "-c", "--db_charset",
        help=f"Database charset, default: {DEFAULT_DB_CHARSET}")
    parser.add_argument(
        "-o", "--output_dir",
        help=f"Output directory, default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument(
        "-l", "--log_level",
        help=f"log level, default: {DEFAULT_LOG_LEVEL}")

    args = parser.parse_args()

    extractor = PC3K_Extractor(args)
    extractor.process()
