/*
 * grokbot-things-helper: Full Disk Access launcher for the Things worker.
 *
 * macOS attaches the Full Disk Access grant to this binary's code signature
 * (CDHash). The Things 3 database lives in an app group container, which TCC
 * protects, so the Python worker can only read it when launched from a
 * granted binary.
 *
 * Because the grant follows this binary, the binary must not become a
 * general-purpose "run anything with FDA" primitive. It therefore:
 *   - takes no arguments and honors no inherited environment except an
 *     optional THINGS_SNAPSHOT_MAX_MB;
 *   - executes one interpreter and one script whose paths are compiled in;
 *   - validates every project file it hands to that interpreter (regular
 *     file, expected owner, not group/world writable);
 *   - in hardened builds, requires all code to be root-owned and validates
 *     the root-owned read allowlist before starting Python;
 *   - runs Python in isolated mode (-I) without writing bytecode (-B).
 *
 * Installers bake every path at compile time:
 *   -DHELPER_SCRIPT, -DWRITE_GATE_SCRIPT, -DCONFIRM_HELPER, -DBRIDGE_ROOT,
 *   -DPYTHON_INTERPRETER, and optionally -DEXPECTED_CODE_UID=0,
 *   -DREAD_POLICY_MODE, -DREAD_ALLOWLIST_PATH, -DREQUIRE_ROOT_POLICY=1.
 */

#include <errno.h>
#include <limits.h>
#include <pwd.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef HELPER_SCRIPT
#error "HELPER_SCRIPT must be defined at build time"
#endif

#ifndef WRITE_GATE_SCRIPT
#error "WRITE_GATE_SCRIPT must be defined at build time"
#endif

#ifndef CONFIRM_HELPER
#error "CONFIRM_HELPER must be defined at build time"
#endif

#ifndef BRIDGE_ROOT
#error "BRIDGE_ROOT must be defined at build time"
#endif

#ifndef PYTHON_INTERPRETER
#define PYTHON_INTERPRETER "/usr/bin/python3"
#endif

/* -1 means "the invoking user"; hardened builds pass 0 (root). */
#ifndef EXPECTED_CODE_UID
#define EXPECTED_CODE_UID -1L
#endif

/* "runtime" lets the worker read policy/read_policy.txt. */
#ifndef READ_POLICY_MODE
#define READ_POLICY_MODE "runtime"
#endif

#ifndef READ_ALLOWLIST_PATH
#define READ_ALLOWLIST_PATH BRIDGE_ROOT "/policy/allowed_scopes.txt"
#endif

#ifndef REQUIRE_ROOT_POLICY
#define REQUIRE_ROOT_POLICY 0
#endif

#ifndef HELPER_DISPLAY_NAME
#define HELPER_DISPLAY_NAME "grokbot-things-helper"
#endif

#ifndef HOST_DISPLAY_NAME
#define HOST_DISPLAY_NAME "Grok Bot"
#endif

extern char **environ;

/*
 * Exit codes: 2 = missing, 3 = not a regular file (or a symlink),
 * 4 = owner mismatch, 5 = group/world writable or readable policy,
 * 6 = not executable, 7 = environment value too long, 64 = usage.
 */
static int validate_file(const char *path, const char *label, uid_t owner,
                         bool require_executable) {
    struct stat st;
    if (lstat(path, &st) != 0) {
        fprintf(stderr, "%s: %s missing at %s (%s)\n", HELPER_DISPLAY_NAME,
                label, path, strerror(errno));
        return 2;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr, "%s: %s %s is not a regular file; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 3;
    }
    if (st.st_uid != owner) {
        fprintf(stderr, "%s: %s %s has uid %u, expected %u; refusing\n",
                HELPER_DISPLAY_NAME, label, path, (unsigned int)st.st_uid,
                (unsigned int)owner);
        return 4;
    }
    if (st.st_mode & (S_IWGRP | S_IWOTH)) {
        fprintf(stderr, "%s: %s %s is group/world writable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 5;
    }
    if (require_executable && !(st.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH))) {
        fprintf(stderr, "%s: %s %s is not executable; refusing\n",
                HELPER_DISPLAY_NAME, label, path);
        return 6;
    }
    return 0;
}

static int set_env_value(char *buffer, size_t size, const char *name,
                         const char *value) {
    int written = snprintf(buffer, size, "%s=%s", name, value);
    if (written < 0 || (size_t)written >= size) {
        fprintf(stderr, "%s: %s value is too long\n", HELPER_DISPLAY_NAME, name);
        return -1;
    }
    return 0;
}

/* Accept only a short run of ASCII digits for the forwarded snapshot limit. */
static bool is_small_positive_integer(const char *value) {
    size_t length = strlen(value);
    if (length == 0 || length > 6) {
        return false;
    }
    for (size_t i = 0; i < length; i++) {
        if (value[i] < '0' || value[i] > '9') {
            return false;
        }
    }
    return true;
}

int main(int argc, char **argv) {
    (void)argv;
    if (argc != 1) {
        fprintf(stderr, "%s: takes no arguments\n", HELPER_DISPLAY_NAME);
        return 64;
    }

    uid_t expected_code_uid =
        EXPECTED_CODE_UID < 0 ? getuid() : (uid_t)EXPECTED_CODE_UID;
    int validation = validate_file(HELPER_SCRIPT, "helper script",
                                   expected_code_uid, false);
    if (validation != 0) {
        return validation;
    }
    validation = validate_file(WRITE_GATE_SCRIPT, "write-gate module",
                               expected_code_uid, false);
    if (validation != 0) {
        return validation;
    }
    validation = validate_file(CONFIRM_HELPER, "confirmation helper",
                               expected_code_uid, true);
    if (validation != 0) {
        return validation;
    }

#if REQUIRE_ROOT_POLICY
    validation = validate_file(READ_ALLOWLIST_PATH, "read allowlist", 0, false);
    if (validation != 0) {
        return validation;
    }
    struct stat policy_st;
    if (lstat(READ_ALLOWLIST_PATH, &policy_st) != 0 ||
        (policy_st.st_mode & (S_IRWXG | S_IRWXO))) {
        fprintf(stderr, "%s: read allowlist has group/world permissions; refusing\n",
                HELPER_DISPLAY_NAME);
        return 5;
    }
#endif

    struct passwd *pw = getpwuid(getuid());
    static char home_buf[PATH_MAX + 16];
    static char bridge_buf[PATH_MAX + 32];
    static char policy_buf[64];
    static char allowlist_buf[PATH_MAX + 32];
    static char root_policy_buf[64];
    static char host_display_buf[128];
    static char snapshot_max_mb_buf[64];

    if (set_env_value(home_buf, sizeof(home_buf), "HOME",
                      pw && pw->pw_dir ? pw->pw_dir : "/") != 0 ||
        set_env_value(bridge_buf, sizeof(bridge_buf), "THINGS_BRIDGE_DIR",
                      BRIDGE_ROOT) != 0 ||
        set_env_value(policy_buf, sizeof(policy_buf), "THINGS_READ_POLICY",
                      READ_POLICY_MODE) != 0 ||
        set_env_value(allowlist_buf, sizeof(allowlist_buf),
                      "THINGS_READ_ALLOWLIST", READ_ALLOWLIST_PATH) != 0 ||
        set_env_value(root_policy_buf, sizeof(root_policy_buf),
                      "THINGS_REQUIRE_ROOT_POLICY",
                      REQUIRE_ROOT_POLICY ? "1" : "0") != 0 ||
        set_env_value(host_display_buf, sizeof(host_display_buf),
                      "THINGS_HOST_DISPLAY_NAME", HOST_DISPLAY_NAME) != 0) {
        return 7;
    }

    static char *new_env[] = {
        "PATH=/usr/bin:/bin",
        home_buf,
        "LANG=en_US.UTF-8",
        bridge_buf,
        policy_buf,
        allowlist_buf,
        root_policy_buf,
        host_display_buf,
        NULL, /* optional THINGS_SNAPSHOT_MAX_MB */
        NULL,
    };

    const char *snapshot_max_mb = getenv("THINGS_SNAPSHOT_MAX_MB");
    if (snapshot_max_mb && is_small_positive_integer(snapshot_max_mb)) {
        if (set_env_value(snapshot_max_mb_buf, sizeof(snapshot_max_mb_buf),
                          "THINGS_SNAPSHOT_MAX_MB", snapshot_max_mb) != 0) {
            return 7;
        }
        new_env[(sizeof(new_env) / sizeof(new_env[0])) - 2] = snapshot_max_mb_buf;
    }
    environ = new_env;

    char *exec_argv[] = {
        (char *)PYTHON_INTERPRETER,
        "-I",
        "-B",
        (char *)HELPER_SCRIPT,
        NULL,
    };
    execv(PYTHON_INTERPRETER, exec_argv);

    fprintf(stderr, "%s: execv %s failed: %s\n", HELPER_DISPLAY_NAME,
            PYTHON_INTERPRETER, strerror(errno));
    return 1;
}
