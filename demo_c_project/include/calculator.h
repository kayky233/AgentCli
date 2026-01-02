#pragma once

#ifdef __cplusplus
extern "C" {
#endif

int add(int a, int b);
int subtract(int a, int b);
int multiply(int a, int b);
int calculator_mod(int a, int b, int* error);
int divide(int a, int b, int* error);

#ifdef __cplusplus
}
#endif

