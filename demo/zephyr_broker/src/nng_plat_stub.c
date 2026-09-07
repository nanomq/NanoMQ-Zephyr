//
// Missing NanoNNG platform symbols for the no-file-system Zephyr build.
//
// NanoNNG's src/platform/zephyr/zephyr_file.c compiles a stub block when
// CONFIG_FILE_SYSTEM is not defined, but that block omits
// nni_plat_file_exists() and nni_plat_file_size().  nanolib's file.c and
// log.c reference them unconditionally, so any consumer of those
// translation units (the broker does, via nano_file_exists) fails to
// link.  The ExternalProject build never defines CONFIG_FILE_SYSTEM
// (Zephyr config macros are not forwarded), so the stub block is always
// selected here.
//
// Provide the two missing symbols with no-filesystem semantics.  No
// broker path that reaches them is enabled by the embedded conf
// (log file rotation and REST file I/O are off by default).
//
// Upstream fix candidate: add these to the no-FS stub block in
// NanoNNG src/platform/zephyr/zephyr_file.c.
//
#include "nng/nng.h"

bool
nni_plat_file_exists(const char *path)
{
	(void) path;
	return (false);
}

int
nni_plat_file_size(const char *path, size_t *sizep)
{
	(void) path;
	(void) sizep;
	return (NNG_ENOTSUP);
}
