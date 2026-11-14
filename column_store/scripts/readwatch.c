/* Minimal LD_PRELOAD shim that records the bytes Python actually reads from
 * one file.  Used by tests/scripts to prove that a narrow scan does not touch
 * unrelated column pages.  Not part of the store implementation.
 *
 *   gcc -shared -fPIC -O2 -o readwatch.so readwatch.c -ldl -lpthread
 *   READWATCH_LOG=/tmp/log READWATCH_PATH=/abs/file LD_PRELOAD=./readwatch.so
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <sys/types.h>

static int logfd = -2;
static const char *onlypath = NULL;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;

__attribute__((constructor)) static void initrw(void) {
    const char *p = getenv("READWATCH_LOG");
    onlypath = getenv("READWATCH_PATH");
    if (!p) return;
    logfd = (int)syscall(SYS_openat, AT_FDCWD, p, O_WRONLY | O_CREAT | O_APPEND,
                         0644);
}

static void logline(const char *op, int fd, ssize_t ret, size_t count,
                    long long off) {
    if (logfd < 0 || fd == logfd) return;
    char link[64];
    char path[4096];
    char out[4352];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t pl = readlink(link, path, sizeof(path) - 1);
    if (pl < 0) return;
    path[pl] = 0;
    if (onlypath && strcmp(path, onlypath) != 0) return;
    int len = snprintf(out, sizeof(out), "%s %zd %zu %lld %s\n", op, ret,
                       count, off, path);
    pthread_mutex_lock(&mu);
    syscall(SYS_write, logfd, out, len);
    pthread_mutex_unlock(&mu);
}

ssize_t read(int fd, void *buf, size_t count) {
    static ssize_t (*real)(int, void *, size_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "read");
    ssize_t r = real(fd, buf, count);
    logline("R", fd, r, count, -1);
    return r;
}

ssize_t pread(int fd, void *buf, size_t count, off_t off) {
    static ssize_t (*real)(int, void *, size_t, off_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "pread");
    ssize_t r = real(fd, buf, count, off);
    logline("P", fd, r, count, (long long)off);
    return r;
}

ssize_t pread64(int fd, void *buf, size_t count, off_t off) {
    static ssize_t (*real)(int, void *, size_t, off_t) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "pread64");
    ssize_t r = real(fd, buf, count, off);
    logline("P64", fd, r, count, (long long)off);
    return r;
}
