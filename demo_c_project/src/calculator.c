#include "calculator.h"
#include <stdlib.h>

int add(int a, int b) {
    return a + b;
}

int subtract(int a, int b) {
    return a - b;
}

int multiply(int a, int b) {
    return a * b;
}

int calculator_mod(int a, int b, int* error) {
    if (b == 0) {
        if (error) *error = 1;
        return 0;
    }
    if (error) *error = 0;
    return a % b;
}

int divide(int a, int b, int* error) {
    if (b == 0) {
        if (error) {
            *error = 1;
        }
        return 0;
    }
    if (error) {
        *error = 0;
    }
    return a / b;
}

static void heap_sift_up(int* heap, int idx) {
    while (idx > 0) {
        int parent = (idx - 1) / 2;
        if (heap[parent] <= heap[idx]) break;
        int tmp = heap[parent];
        heap[parent] = heap[idx];
        heap[idx] = tmp;
        idx = parent;
    }
}

static void heap_sift_down(int* heap, int size, int idx) {
    while (1) {
        int left = idx * 2 + 1;
        int right = idx * 2 + 2;
        int smallest = idx;
        if (left < size && heap[left] < heap[smallest]) smallest = left;
        if (right < size && heap[right] < heap[smallest]) smallest = right;
        if (smallest == idx) break;
        int tmp = heap[smallest];
        heap[smallest] = heap[idx];
        heap[idx] = tmp;
        idx = smallest;
    }
}

void min_heap_insert(int** heap, int* size, int* capacity, int value) {
    if (*capacity == 0) {
        *capacity = 10;
        *heap = (int*)malloc(*capacity * sizeof(int));
    } else if (*size >= *capacity) {
        *capacity *= 2;
        *heap = (int*)realloc(*heap, *capacity * sizeof(int));
    }
    (*heap)[*size] = value;
    (*size)++;
    heap_sift_up(*heap, *size - 1);
}

int min_heap_delete_min(int** heap, int* size) {
    if (*size == 0) {
        return -1; // empty queue
    }
    int min = (*heap)[0];
    (*heap)[0] = (*heap)[*size - 1];
    (*size)--;
    if (*size > 0) {
        heap_sift_down(*heap, *size, 0);
    }
    return min;
}

int min_heap_peek_min(int* heap, int size) {
    if (size == 0) {
        return -1;
    }
    return heap[0];
}

void destroy_queue(int** heap, int* size, int* capacity) {
    if (heap && *heap) {
        free(*heap);
        *heap = NULL;
    }
    if (size) *size = 0;
    if (capacity) *capacity = 0;
}

