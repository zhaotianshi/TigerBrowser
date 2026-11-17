// Compile with upstream ffmpeg.cxx, -std=c++20, -Wl,--wrap=dlopen and -rdynamic.
#include <rfb/ffmpeg.h>
#include <rfb/LogWriter.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <sys/stat.h>

namespace {
struct Library {
    const char *soname;
    const char *version_symbol;
    unsigned major;
};

constexpr Library libraries[] = {
    {"libavformat.so." STR(LIBAVFORMAT_VERSION_MAJOR), "avformat_version", LIBAVFORMAT_VERSION_MAJOR},
    {"libavutil.so." STR(LIBAVUTIL_VERSION_MAJOR), "avutil_version", LIBAVUTIL_VERSION_MAJOR},
    {"libswscale.so." STR(LIBSWSCALE_VERSION_MAJOR), "swscale_version", LIBSWSCALE_VERSION_MAJOR},
    {"libavcodec.so." STR(LIBAVCODEC_VERSION_MAJOR), "avcodec_version", LIBAVCODEC_VERSION_MAJOR},
};
unsigned attempts;
unsigned directory_reads;
unsigned missing_library;
bool wrong_library;
bool wrong_abi;

int fail(const char *reason) {
    std::fprintf(stderr, "FFmpeg loader regression: %s\n", reason);
    return 1;
}
}

// Logging is disconnected; all library loading and symbol lookup remain upstream code.
rfb::LogWriter::LogWriter(const char *name)
    : m_name(name), m_level(0), m_log(nullptr), m_next(nullptr) {}
rfb::LogWriter::~LogWriter() = default;
void rfb::Logger::write(int, const char *, const char *, va_list) {
    std::abort();
}

extern "C" void *__real_dlopen(const char *, int);
extern "C" void *__wrap_dlopen(const char *filename, int flags) {
    const unsigned index = attempts++;
    if (index >= 4 || !filename || std::strcmp(filename, libraries[index].soname)) {
        wrong_library = true;
        return nullptr;
    }
    if (missing_library == index + 1)
        return nullptr;
    void *handle = __real_dlopen(filename, flags);
    if (handle) {
        const auto version = reinterpret_cast<unsigned (*)()>(dlsym(handle, libraries[index].version_symbol));
        if (!version || AV_VERSION_MAJOR(version()) != libraries[index].major)
            wrong_abi = true;
    }
    return handle;
}

// libstdc++ opens directories from its DSO, so linker --wrap alone cannot intercept it.
extern "C" DIR *opendir(const char *path) {
    if (!std::strcmp(path, "/usr/lib") || !std::strncmp(path, "/usr/lib/", 9)
        || !std::strcmp(path, "/usr/lib64") || !std::strncmp(path, "/usr/lib64/", 11)) {
        ++directory_reads;
        errno = EACCES;
        return nullptr;
    }
    const auto original = reinterpret_cast<DIR *(*)(const char *)>(dlsym(RTLD_NEXT, "opendir"));
    if (!original)
        std::abort();
    return original(path);
}

extern "C" DIR *fdopendir(int fd) {
    struct stat opened{}, root{};
    if (fstat(fd, &opened))
        return nullptr;
    for (const char *path : {"/usr/lib", "/usr/lib64"}) {
        if (!stat(path, &root) && opened.st_dev == root.st_dev && opened.st_ino == root.st_ino) {
            ++directory_reads;
            errno = EACCES;
            return nullptr;
        }
    }
    const auto original = reinterpret_cast<DIR *(*)(int)>(dlsym(RTLD_NEXT, "fdopendir"));
    if (!original)
        std::abort();
    return original(fd);
}

int main(int argc, char **argv) {
    if (argc > 2 || (argc == 2 && (std::strlen(argv[1]) != 1 || argv[1][0] < '1' || argv[1][0] > '4')))
        return fail("expected no argument or missing-library index 1..4");
    if (argc == 2)
        missing_library = static_cast<unsigned>(argv[1][0] - '0');
    const bool available = FFmpeg::get().is_available();
    if (directory_reads)
        return fail("system library directories were enumerated");
    if (wrong_library || wrong_abi)
        return fail("loader requested an unexpected SONAME or loaded a different ABI");
    if (attempts != (missing_library ? missing_library : 4))
        return fail("loader did not stop at the missing library or load all four libraries");
    if (available != !missing_library)
        return fail("FFmpeg availability does not match the injected library failure");
    std::puts("FFmpeg loader regression passed");
    return 0;
}
