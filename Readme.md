# About

This is a tiny Python program to extract blob data from ACELab PC3000 Firebird database.
It attempts to recreate the source "folder" structure from the database and
uncompress zlib'ed blob data.

Similar projects:
- https://github.com/aekhv/ace-database-viewer

# Install
1. Create and activate a virtualenv, install dependencies
```
virtualenv -p python3 .
source bin/activate
pip3 install -r requirements.txt
```
2. Create an input directory and put your Firebird databse file (`.gdb`) there.
The program reads the source DB from `input/pc3k.gdb`:
```
mkdir input
cp pc3k.gdb input/

```

# Restoring Firebird database backup
If you have a Firebird database backup (`.gbk` file) - you will need to restore it:
1. Install firebird utils:
```
sudo apt-get install firebird-utils
```
2. Convert gbk->gdb:
```
gbak -c -v -user SYSDBA -password 'masterkey' pc3k.gbk pc3k.gdb
```

# Executing
1. Run the extractor (after activating the virtualenv, see p.1 from Install section):
```
python pc3k_extractor.py 1>log.txt 2>err.txt

```

## Additional info
- Resulting filenames are sanitized and should be compatible with both Win/NTFS and Linux/Ext4.
- Default DB encoding is CP1251.
- Check `python pc3k_extractor.py -h` for supported options.

### DB and file path conflicts
It is possible to encounter some issues when exporting, the program attempts to fix some of them:
- Duplicate filenames:
  - Only data from a DB entry with newest update time is exported.
- Conflicts when parent dir already exists:
  - Child blobs are stored in `${PARENT}_` directory.
- Blobs with broken zlib compression:
  - Such blobs are exported "as is" - without decompressing the data.
  - Not sure why it happens. On some DB there are 50-100 such blobs per 500k+ records.

### Donations
Donations are accepted at `Bitcoin:bc1qpxpzjc0c7ug5dw3yg87jyqr0lc674aqymwmzsf`
