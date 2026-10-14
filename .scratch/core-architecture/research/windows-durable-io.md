# Durable, correct file I/O on Windows (and Linux)

Research for ticket `.scratch/core-architecture/issues/04-windows-durable-io.md`.
Sources accessed 2026-10-14. Every claim cites the page or source file that owns it.
Where a source does not settle a question, it says so.

## TL;DR

- **Durability primitive on Windows is `FlushFileBuffers`** (Rust: `File::sync_all` *and* `File::sync_data`, which both call it). It flushes data and metadata of that file and, per the NT flush semantics, asks the device to flush its cache. [R1][M6]
- **Cheaper WAL commit path:** open the WAL with `FILE_FLAG_WRITE_THROUGH` (optionally plus `FILE_FLAG_NO_BUFFERING`). With both, Windows sends the write past the system cache and requests write-through of the drive cache (FUA). This is what SQL Server does for both data and log files. [M2][M8]
- **`FILE_FLAG_NO_BUFFERING` needs alignment:** I/O sizes and offsets must be multiples of the volume sector size; buffers should be aligned to the physical sector size. Query `FILE_STORAGE_INFO` via `GetFileInformationByHandleEx(FileStorageInfo)` (Win8+). Page-aligned (4 KiB) buffers satisfy this in practice. [M3][M4]
- **No sector-atomicity guarantee you can lean on.** Windows reports a `PhysicalBytesPerSectorForAtomicity`, but SQLite deliberately assumes sector writes are *not* atomic, and Microsoft lists "shorn writes" as a real SSD power-fault mode. Design the WAL and pages to detect torn writes (checksums), not to rely on atomicity. [M4][S1][M8]
- **Single-process ownership:** take an exclusive `LockFileEx` (Rust 1.89+: `File::try_lock`) on a lock file **and** open the data/WAL files with a restrictive `dwShareMode` (Rust's default shares read/write/delete). Windows byte-range locks are mandatory for `ReadFile`/`WriteFile` but are ignored by mapped views. Linux `flock` is advisory only. [M5][M1][R2][L3]
- **mmap:** `FlushViewOfFile` alone is not durable; you must follow it with `FlushFileBuffers` (memmap2's `flush()` does this). You cannot truncate a file while it is mapped. Data-loss and error handling via page faults is the main reason DBMS experts advise against mmap for the buffer pool. [M7][M9][C1][P1]
- **Pitfalls:** antivirus filter drivers open your files (sharing violations, slow I/O; SQL Server can mark a DB suspect); drive write caches can lie about flushes; `FlushFileBuffers` can be disabled by registry settings on some Windows versions. [M10][M8][S1]
- **Linux differences:** `fsync` does not make a new directory entry durable (fsync the directory too). After an fsync error the kernel may have dropped the dirty pages, and the error is reported only once. Treat any fsync failure as fatal (PostgreSQL PANICs). Windows behavior after a failed flush is undocumented, so treat it the same way. [L1][P2]

## 1. Flushing to stable storage

### Windows

| Mechanism | What it guarantees | Source |
| --- | --- | --- |
| Default `WriteFile` | Goes to the system cache (write-back); the lazy writer flushes "at intervals determined by the operating system". | [M2] |
| `FlushFileBuffers(h)` | "writes all the buffered information for a specified file to the device". Needs `GENERIC_WRITE`. It "can be inefficient when used after every write". | [R1] |
| `FILE_FLAG_WRITE_THROUGH` only | "data is written to the system cache but is flushed to disk without delay". Also "causes NTFS to flush any metadata changes" from the request. | [M1] |
| `WRITE_THROUGH` + `NO_BUFFERING` | Data goes straight to disk, bypassing the system cache, and "the operating system also requests a write-through of the hard disk's local hardware cache". Note: "Not all hard disk hardware supports this write-through capability". Microsoft offers this combination "as a replacement for calling FlushFileBuffers after each write". | [M1] |
| `NO_BUFFERING` only | Bypasses the data cache, but "the file metadata may still be cached ... use FlushFileBuffers" (for example, when the file is extended). | [M1][M2] |
| `NtFlushBuffersFileEx(h, flags)` (Win8+, ntdll) | Flag `0` = data + metadata + storage cache sync (what `FlushFileBuffers` does). `FLUSH_FLAGS_FILE_DATA_SYNC_ONLY` = data plus only the metadata needed to read it back, with a storage sync. This is the closest analogue to `fdatasync`, but it is **NTFS only**. `FLUSH_FLAGS_NO_SYNC` / `FILE_DATA_ONLY` skip the device-cache flush, so they are not durable. | [M6] |

The SQL Server docs say it opens all data and log files with `FILE_FLAG_WRITE_THROUGH`. This "ensures that when a write operation returns successful completion, the data is correctly stored in stable storage" (FUA on Windows). The same page warns that on-device caches "can't guarantee writes across a power cycle", and it requires battery- or capacitor-backed caching or a disabled drive cache. [M8]

**Rename/replace durability:** `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`. The docs say `WRITE_THROUGH` makes the function not return "until the file is actually moved on the disk". The page does not promise that the replace is atomic. [M11]

### Linux

- `fsync` flushes data and metadata and "includes writing through or flushing a disk cache if present". `fdatasync` skips metadata that is not needed to read the data back, such as mtime, but not file size. [L1]
- "Calling fsync() does not necessarily ensure that the entry in the directory containing the file has also reached disk". You need an explicit fsync on the directory fd after you create or rename a file. Windows has no documented equivalent step. Rely on `FlushFileBuffers` of the file plus `MOVEFILE_WRITE_THROUGH` for renames. [L1][M11]
- `O_DSYNC` / `O_SYNC` are the per-open analogues of write-through. `RWF_DSYNC` (Linux 4.7+, `pwritev2`) is a per-write version of `O_DSYNC`. [L2][L4]
- **fsync error semantics ("fsyncgate").** On Linux 4.13+ the error is reported once, the pages are marked clean, and "retrying fsync() is not OK". You "might read back an older version of the page than you most recently wrote". PostgreSQL now PANICs on fsync failure. The PostgreSQL wiki lists Windows behavior as "unknown". [P2]

### Rust std

- `File::sync_all` on Windows calls `FlushFileBuffers`. `File::sync_data` on Windows simply calls `self.fsync()`, so it is also `FlushFileBuffers` (no `fdatasync` analogue). Source: `library/std/src/sys/fs/windows.rs`. [R3]
- On Unix, `sync_all` calls `fsync`, or `fcntl(F_FULLFSYNC)` on Apple platforms. `sync_data` calls `fdatasync` on Linux, Android, FreeBSD and others, and falls back to `fsync` elsewhere. Source: `library/std/src/sys/fs/unix.rs`. [R4]
- Write-through and unbuffered flags are set through `std::os::windows::fs::OpenOptionsExt::custom_flags(u32)`, which adds `dwFlagsAndAttributes` bits. On Linux, use `std::os::unix::fs::OpenOptionsExt::custom_flags(libc::O_DIRECT | libc::O_DSYNC)`. [R2]
- `NtFlushBuffersFileEx` is exposed by `windows-sys` at `windows_sys::Wdk::Storage::FileSystem::NtFlushBuffersFileEx`. [C2]

## 2. Unbuffered / direct I/O and alignment

### Windows `FILE_FLAG_NO_BUFFERING` [M3]

- I/O sizes **and** the file offset in `OVERLAPPED` must be integer multiples of the **volume sector size**.
- Buffer addresses "should be physical sector-aligned ... Depending on the disk, this requirement may not be enforced".
- There are two sector sizes on 512e drives. *Logical* is the addressing unit. *Physical* is "the unit of atomic write, and what unbuffered I/O will need to be aligned to". Microsoft "strongly recommends" aligning to the physical size.
- `VirtualAlloc` returns page-aligned (4 KiB on x86/x64) memory, so "in most situations, page-aligned memory will also be sector-aligned".
- How to get the sizes: `GetFileInformationByHandleEx(FileStorageInfo)` returns `FILE_STORAGE_INFO` (Win8+). Its fields include `LogicalBytesPerSector` ("smallest size for which uncached I/O is supported"), `PhysicalBytesPerSectorForAtomicity`, `PhysicalBytesPerSectorForPerformance` and `FileSystemEffectivePhysicalBytesPerSectorForAtomicity`. On RAID or mirrored volumes it returns the largest value among the underlying devices. [M4] The older path is `IOCTL_STORAGE_QUERY_PROPERTY` → `STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR.BytesPerPhysicalSector`. [M3]

### Linux `O_DIRECT` [L2]

- "alignment restrictions vary by filesystem and kernel version and might be absent entirely". Misaligned I/O may "fail with EINVAL or fall back to buffered I/O". Windows misaligned unbuffered I/O fails instead.
- Since Linux 6.1, query with `statx(STATX_DIOALIGN)`, though support varies by filesystem.
- `O_DIRECT` "does not give the guarantees of the O_SYNC flag". To get synchronous durable I/O you must add `O_SYNC`/`O_DSYNC` or fsync. This matches Windows, where `NO_BUFFERING` without `WRITE_THROUGH` is not durable for metadata. [L2][M1]

**Implication:** a single "aligned block I/O" layer with a 4096-byte unit and page-aligned buffers satisfies both platforms on mainstream hardware. Read the actual unit at open time (`FILE_STORAGE_INFO` / `STATX_DIOALIGN`) and refuse to run, or fall back to buffered I/O, if it is larger.

## 3. Sector / atomic-write guarantees

- Windows reports `PhysicalBytesPerSectorForAtomicity`: "Writes smaller than this may require a read before the entire block can be written atomically." [M4] This is a device-reported property, not a filesystem promise for arbitrary multi-sector writes.
- SQLite "assume[s] that sector writes are not atomic" on both its Unix and Windows VFSes. It does assume "powersafe overwrite", meaning a write does not damage bytes outside its range. It notes that there is "no standard way of discovering the true sector size". [S1]
- Microsoft lists SSD power-fault modes that include "**Shorn writes**: operations are partially done at a level below the expected sector size" and "flying writes". It also notes that 512e SSDs use 4 KiB internal pages, so 512-byte-aligned log writes cause read-modify-write. [M8]
- Linux 6.11 added `RWF_ATOMIC`: torn-write protection for power-of-two, naturally aligned writes within the statx `stx_atomic_write_unit_min/max` range. It works **only with O_DIRECT** and needs O_SYNC/O_DSYNC for consistency. Windows has no documented user-mode equivalent. [L4]

**Implication:** checksum every WAL record and page, and treat a torn tail as end-of-log during recovery. Do not rely on any atomic-write unit on Windows. A full-page-image or double-write scheme is needed for page writes larger than the atomic unit (the design belongs to the WAL ticket).

## 4. File locking and single-process ownership (Embedded mode)

### Windows

- `LockFileEx` takes exclusive or shared byte-range locks. With `LOCKFILE_FAIL_IMMEDIATELY` it does not block. Locking beyond EOF "is not an error". [M5]
- The locks are **mandatory** for normal I/O: an exclusive lock "denies all other processes both read and write access to the specified region". But "Locking a region of a file does not prevent reading or writing from a mapped file view". [M5]
- Locks belong to the handle. A child that inherits the handle "is not granted access", and the same process opening a second handle cannot access the region through it. [M5]
- On process exit or handle close the OS releases locks, but "the time it takes ... depends upon available system resources", so unlock explicitly. A fast restart can briefly see a stale lock. [M5]
- **Share modes** are a second, coarser lock. With `dwShareMode = 0`, the file "cannot be opened again until the handle ... is closed". Conflicts fail with `ERROR_SHARING_VIOLATION`. `FILE_SHARE_DELETE` controls delete and rename. [M1]
- Rust's `OpenOptions` on Windows defaults to `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`. Override it with `OpenOptionsExt::share_mode`. [R2][R3]

### Linux

- `flock` "places advisory locks only". It is tied to the open file description: fork and dup share it, while a separate `open()` is independent. On Linux it does not interact with `fcntl` locks. Over NFS it is emulated with `fcntl` locks. [L3]

### Rust std (stable since 1.89.0)

- `File::lock` / `lock_shared` / `try_lock` / `try_lock_shared` / `unlock` map to `flock` on Unix and to `LockFileEx` with the range `(0, u32::MAX:u32::MAX)` on Windows. `try_lock` returns `TryLockError::WouldBlock` on `ERROR_LOCK_VIOLATION`. `unlock` calls `UnlockFile` twice to drop both exclusive and shared locks. [R3][R5]
- The docs warn that locks "may be advisory or mandatory" depending on platform. On Windows, locking fails if the file is opened only for append. [R5]

**Implication:** for Embedded mode, open a dedicated lock file (or the main file) and call `try_lock()` (exclusive). Fail fast with a clear "database in use" error. On Windows, also open the data and WAL files with `share_mode` that excludes `FILE_SHARE_WRITE | FILE_SHARE_DELETE`. That gives OS-enforced protection against other writers, deletes and renames, which Linux cannot offer. Do not rely on byte-range locks if the engine mmaps the locked region. [M5] Both SQLite and Microsoft warn that locking on network filesystems can be broken, so document "local disks only". [S1]

## 5. Memory-mapped files

- `FlushViewOfFile` "does not flush the file metadata, and it does not wait to return until the changes are flushed from the underlying hardware disk cache". For durability, "call FlushViewOfFile and then call the FlushFileBuffers function". Over SMB it only guarantees the data left the local machine. [M7]
- `memmap2::MmapMut::flush()` on Windows does exactly that: `FlushViewOfFile`, then `FlushFileBuffers` on the file handle if it has one. `flush_async` only calls `FlushViewOfFile`. [C1]
- Creating a file-backed map is `unsafe` in memmap2 because modification of the file "in or out of process" is UB. [C1b]
- Windows cannot shrink or extend a file with `SetEndOfFile` while mapped: all views must be unmapped and the mapping handle closed first. [M9] Linux allows `ftruncate` under a live mapping (access past EOF gives SIGBUS). This makes growing a mapped file a platform-divergent code path.
- Crotty, Leis and Pavlo (CIDR 2022) argue mmap has "severe correctness and performance issues" for DBMS buffer pools, and that several DBMSs that started with mmap later replaced it at significant engineering cost. [P1] (The specific issues, such as uncontrolled write-back ordering and I/O errors arriving as faults, are in the full paper, which was not re-read here. See Open.)

**Implication:** use explicit `pread`/`pwrite` (`seek_read`/`seek_write` on Windows) with our own buffer pool. If mmap is used at all, keep it read-only.

## 6. Windows pitfalls

- **Antivirus / filter drivers.** AV products attach file-system filter drivers to the I/O path. Microsoft says AV scanning causes I/O latency, and "If the virus sweep software opens a database file when SQL Server tries to open the database, the database ... might be marked as suspect". Its advice is to exclude data and log extensions and folders. `fltmc instances` lists the filters attached to a volume. [M10][M8] For us, opens can fail with sharing violations because scanners open the file, and that is more likely with a restrictive share mode. Retry open/rename on `ERROR_SHARING_VIOLATION` / `ERROR_ACCESS_DENIED` with a short backoff, and document recommended AV exclusions.
- **Lying hardware.** SQLite reports that "FlushFileBuffers() can be completely disabled using registry settings on some Windows versions" and that drives may acknowledge writes still held in a volatile cache. [S1] Microsoft asks for "guaranteed delivery to stable media" and recommends validating with SQLIOSim/diskspd. [M8] This is out of our control: document it, and add a crash-consistency test harness.
- **`FILE_ATTRIBUTE_TEMPORARY`** tells the cache manager to avoid writing data back. Never set it on durable files. [M1]
- **Extending files.** `SetEndOfFile` extension leaves the new region's contents "not defined" until written. Valid data length is separate (`SetFileValidData`). Extension also changes metadata, so after growing a file, call `FlushFileBuffers` (or use write-through). [M9][M2] Pre-allocating WAL segments avoids metadata updates on the commit path.
- **Network shares (SMB).** Flush semantics are weaker over the network (see `FlushViewOfFile`), and locking can be subtly broken. [M7][S1] Support local volumes only.

## 7. Rust crates

| Need | Crate / API | Notes |
| --- | --- | --- |
| fsync / flush | `std::fs::File::sync_all`, `sync_data` | Windows: both are `FlushFileBuffers`. [R3] |
| Open flags, share mode | `std::os::windows::fs::OpenOptionsExt` (`custom_flags`, `share_mode`, `access_mode`, `attributes`); `std::os::unix::fs::OpenOptionsExt::custom_flags` | [R2] |
| Positional I/O | `std::os::windows::fs::FileExt::seek_read/seek_write`; `std::os::unix::fs::FileExt::read_at/write_at` | std |
| Whole-file locks | `std::fs::File::lock/try_lock/...` (Rust ≥ 1.89) | Replaces `fs2`/`fs4` for our needs. [R5] |
| Raw Win32/NT (`FILE_STORAGE_INFO`, `NtFlushBuffersFileEx`, `LockFileEx` ranges, `FILE_FLAG_*` constants) | `windows-sys` | [C2] |
| Raw POSIX (`fdatasync`, `statx`, `O_DIRECT`) | `rustix` (`fs` feature) or `libc` | rustix docs cite only POSIX/Linux references. [C3] |
| mmap | `memmap2` | `flush()` = `FlushViewOfFile` + `FlushFileBuffers` on Windows; constructors are `unsafe`. [C1][C1b] |

## 8. Suggested defaults for the vertical slice

These follow from the facts above and are proposals for the WAL/storage tickets, not decisions:

1. Use the WAL with `FILE_FLAG_WRITE_THROUGH` (Windows) and `O_DSYNC` (Linux). Write pre-allocated segments in 4 KiB-aligned blocks. Consider adding `NO_BUFFERING`/`O_DIRECT` later as an optimization, behind the same aligned-buffer abstraction.
2. For data files, use buffered I/O plus `sync_all()` at checkpoint.
3. On Linux, fsync the directory after creating or renaming files. On Windows, use `MoveFileEx(..., WRITE_THROUGH)`.
4. Checksum every WAL record and page. A torn WAL tail means end of log.
5. Any flush error is fatal: crash and recover from the WAL. Never retry fsync.
6. For Embedded ownership, use `File::try_lock()` on a lock file, plus a restrictive `share_mode` on Windows.
7. Avoid mmap for the buffer pool.

## Sources

- [M1] CreateFileW (Caching Behavior, dwShareMode): https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
- [M2] File Caching: https://learn.microsoft.com/en-us/windows/win32/fileio/file-caching
- [M3] File Buffering (alignment): https://learn.microsoft.com/en-us/windows/win32/fileio/file-buffering
- [M4] FILE_STORAGE_INFO: https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_storage_info
- [M5] LockFileEx: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
- [M6] NtFlushBuffersFileEx: https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntflushbuffersfileex
- [M7] FlushViewOfFile: https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-flushviewoffile
- [M8] SQL Server I/O Fundamentals: https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-storage-guide
- [M9] SetEndOfFile: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setendoffile
- [M10] Configure antivirus software to work with SQL Server: https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/security/antivirus-and-sql-server
- [M11] MoveFileExW: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
- [R1] FlushFileBuffers: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
- [R2] Rust `std::os::windows::fs::OpenOptionsExt`: https://doc.rust-lang.org/std/os/windows/fs/trait.OpenOptionsExt.html
- [R3] Rust std source, Windows fs: https://github.com/rust-lang/rust/blob/master/library/std/src/sys/fs/windows.rs
- [R4] Rust std source, Unix fs: https://github.com/rust-lang/rust/blob/master/library/std/src/sys/fs/unix.rs
- [R5] Rust `std::fs::File` (lock APIs, sync_all/sync_data): https://doc.rust-lang.org/std/fs/struct.File.html
- [L1] fsync(2): https://man7.org/linux/man-pages/man2/fsync.2.html
- [L2] open(2) (O_DIRECT, O_SYNC, O_DSYNC): https://man7.org/linux/man-pages/man2/open.2.html
- [L3] flock(2): https://man7.org/linux/man-pages/man2/flock.2.html
- [L4] pwritev2 / readv(2) (RWF_ATOMIC, RWF_DSYNC): https://man7.org/linux/man-pages/man2/readv.2.html
- [S1] SQLite, Atomic Commit (sections 2, 7.4, 9.1, 9.2): https://www.sqlite.org/atomiccommit.html
- [P1] Crotty, Leis, Pavlo, "Are You Sure You Want to Use MMAP in Your Database Management System?", CIDR 2022: https://db.cs.cmu.edu/mmap-cidr2022/
- [P2] PostgreSQL wiki, Fsync Errors: https://wiki.postgresql.org/wiki/Fsync_Errors
- [C1] memmap2 Windows source (flush): https://github.com/RazrFalcon/memmap2-rs/blob/master/src/windows.rs
- [C1b] memmap2 `MmapMut` docs: https://docs.rs/memmap2/latest/memmap2/struct.MmapMut.html
- [C2] windows-sys `NtFlushBuffersFileEx`: https://docs.rs/windows-sys/latest/windows_sys/Wdk/Storage/FileSystem/fn.NtFlushBuffersFileEx.html
- [C3] rustix `fdatasync`: https://docs.rs/rustix/latest/rustix/fs/fn.fdatasync.html

## Open / not verified

- What Windows does after a failed `FlushFileBuffers` (are dirty pages kept or dropped?) is not documented. The PostgreSQL wiki lists it as "unknown". [P2]
- Whether NTFS makes `MOVEFILE_REPLACE_EXISTING` atomic across a crash is not stated in the docs. [M11]
- The claims about the mmap paper come from the paper's landing page summary plus well-known content. Read the full PDF before quoting specifics.
