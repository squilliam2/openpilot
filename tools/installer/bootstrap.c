#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#include "installer_script.h"

int main(int argc, char **argv) {
  (void)argv;
  if (argc != 1) {
    fputs("This installer accepts no arguments.\n", stderr);
    return 64;
  }
  umask(0077);
  if (clearenv() || setenv("PATH", "/usr/local/venv/bin:/usr/bin:/bin", 1) ||
      setenv("HOME", "/home/comma", 1) || setenv("LANG", "C", 1) ||
      setenv("GIT_CONFIG_NOSYSTEM", "1", 1) || setenv("GIT_CONFIG_GLOBAL", "/dev/null", 1) ||
      setenv("GIT_TERMINAL_PROMPT", "0", 1) || setenv("PYTHONDONTWRITEBYTECODE", "1", 1)) {
    perror("Installer environment");
    return 70;
  }
  execl("/bin/bash", "bash", "--noprofile", "--norc", "-c", installer_script, (char *)NULL);
  perror("Installer shell");
  return 71;
}
