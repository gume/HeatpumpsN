// Minimal Arduino stand-in for host tests of the control and storage layers.
#pragma once
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
uint32_t millis();
uint32_t micros();

struct SerialStub {
  void begin(unsigned long) {}
  void println(const char* s) { ::printf("%s\n", s); }
  void println() { ::printf("\n"); }
  void printf(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    ::vprintf(fmt, args);
    va_end(args);
  }
};

inline SerialStub Serial;
