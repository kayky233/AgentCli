#pragma once

#ifdef __cplusplus
extern "C" {
#endif

int add(int a, int b);
int subtract(int a, int b);
int multiply(int a, int b);
int calculator_mod(int a, int b, int* error);
int divide(int a, int b, int* error);

void min_heap_insert(int** heap, int* size, int* capacity, int value);
int min_heap_delete_min(int** heap, int* size);
int min_heap_peek_min(int* heap, int size);
void destroy_queue(int** heap, int* size, int* capacity);

#ifdef __cplusplus
}
#endif

