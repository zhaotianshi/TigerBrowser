/* Exercise the actual patched server address helper with C and C++. */
#include "PrivateSocket.h"

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "%s:%d: %s failed\n", __FILE__, __LINE__, #condition); \
        return 1; \
    } \
} while (0)

int main(void)
{
    struct sockaddr_un address, actual;
    socklen_t length = kasm_private_socket_address(&address, getpid());
    socklen_t actual_length = sizeof(actual);
    CHECK(address.sun_family == AF_UNIX);
    CHECK(strncmp(address.sun_path, "/tmp/.KasmVNCSock", 17) == 0);
    CHECK(length == offsetof(struct sockaddr_un, sun_path) + strlen(address.sun_path) + 1);

    umask(077);
    int marker = open(address.sun_path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    CHECK(marker >= 0);
    CHECK(write(marker, "x", 1) == 1);
    CHECK(close(marker) == 0);
    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    CHECK(listener >= 0);
    errno = 0;
    CHECK(bind(listener, (struct sockaddr *)&address, length) == -1);
    CHECK(errno == EADDRINUSE);
    struct stat original;
    CHECK(lstat(address.sun_path, &original) == 0);
    CHECK(S_ISREG(original.st_mode) && original.st_size == 1);
    CHECK(unlink(address.sun_path) == 0);

    CHECK(bind(listener, (struct sockaddr *)&address, length) == 0);
    CHECK(getsockname(listener, (struct sockaddr *)&actual, &actual_length) == 0);
    CHECK(actual_length == length && strcmp(actual.sun_path, address.sun_path) == 0);
    CHECK(listen(listener, 1) == 0);
    int client = socket(AF_UNIX, SOCK_STREAM, 0);
    CHECK(client >= 0);
    CHECK(connect(client, (struct sockaddr *)&address, length) == 0);
    int accepted = accept(listener, NULL, NULL);
    CHECK(accepted >= 0);
    CHECK(write(client, "y", 1) == 1);
    char received = 0;
    CHECK(read(accepted, &received, 1) == 1 && received == 'y');
    CHECK(close(accepted) == 0 && close(client) == 0 && close(listener) == 0);
    CHECK(unlink(address.sun_path) == 0);
    puts("private IPC filesystem binding and foreign-file preservation: OK");
    return 0;
}
