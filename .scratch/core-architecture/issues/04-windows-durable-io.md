# How do we get durable, correct file I/O on Windows (and Linux)?

Type: research
Status: resolved
Blocked by: 

## Question

Windows durable-I/O facts for a WAL-based engine: FlushFileBuffers, FILE_FLAG_WRITE_THROUGH, FILE_FLAG_NO_BUFFERING (alignment rules), sector/atomic-write guarantees, file locking (LockFileEx) for single-process ownership in Embedded mode, memory-mapped files, and pitfalls such as antivirus and file-sharing modes. How does each one differ from Linux (fsync/fdatasync, O_DIRECT, flock)? What do Rust std and crates expose?

## Answer

- Durability on Windows: `FlushFileBuffers`. In Rust, both `File::sync_all` and `sync_data` call it; Windows has no `fdatasync` analogue except the NTFS-only `NtFlushBuffersFileEx(FLUSH_FLAGS_FILE_DATA_SYNC_ONLY)`, exposed in `windows-sys`. Linux: `fsync`/`fdatasync`, plus an fsync of the directory after creating or renaming a file.
- WAL commit path: open the file with `FILE_FLAG_WRITE_THROUGH` (FUA; this is what SQL Server does), optionally with `FILE_FLAG_NO_BUFFERING`. The Linux equivalent is `O_DSYNC` (optionally with `O_DIRECT`). Set the flags with `OpenOptionsExt::custom_flags`.
- `NO_BUFFERING` and `O_DIRECT` both need sector-multiple sizes and offsets and sector-aligned buffers. Query the sizes with `GetFileInformationByHandleEx(FileStorageInfo)` or `statx(STATX_DIOALIGN)`. A 4 KiB unit with page-aligned buffers works in practice. Misaligned I/O fails on Windows but may silently fall back to buffered I/O on Linux.
- Sector writes cannot be assumed atomic (SQLite's position; Microsoft documents "shorn writes" on SSDs). Checksum every WAL record and page, and treat a torn tail as end of log. Linux `RWF_ATOMIC` (6.11+, O_DIRECT only) has no Windows equivalent.
- Embedded single-process ownership: use `File::try_lock()` (std 1.89+, which is `LockFileEx` on Windows and `flock` on Linux) on a lock file. On Windows, also open data and WAL files with a restrictive `share_mode`; Rust's default shares read, write and delete. Windows locks are mandatory for read/write but ignored by mapped views. `flock` is advisory only.
- mmap: on Windows you need `FlushViewOfFile` followed by `FlushFileBuffers` (memmap2's `flush()` does both), and you cannot resize a file while it is mapped. Avoid mmap for the buffer pool.
- Treat any flush error as fatal: crash and recover from the WAL, and never retry an fsync (Linux drops the dirty pages; Windows behavior is undocumented).
- Pitfalls: antivirus filter drivers cause sharing violations and latency (retry on `ERROR_SHARING_VIOLATION`, document exclusions), drive caches can lie about flushes, `FILE_ATTRIBUTE_TEMPORARY` suppresses write-back, and network shares are unsupported. Use `MoveFileEx(..., MOVEFILE_WRITE_THROUGH)` for durable renames.
- Open: Windows behavior after a failed flush is undocumented; crash-atomicity of `MOVEFILE_REPLACE_EXISTING` is undocumented.

Full findings: [research/windows-durable-io.md](../research/windows-durable-io.md)
