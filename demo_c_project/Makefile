CC ?= gcc
CXX ?= g++
CFLAGS ?= -Wall -Wextra -g -Iinclude
CXXFLAGS ?= $(CFLAGS) -std=c++17 -Ithird_party/minigtest
LDFLAGS ?=

BUILD_DIR := build
OBJ_DIR := $(BUILD_DIR)/obj
BIN_DIR := $(BUILD_DIR)/bin
TEST_DIR := $(BUILD_DIR)/tests

APP := $(BIN_DIR)/demo_app
TEST_BIN := $(TEST_DIR)/runTests

MAIN_SRC := src/main.c
LIB_SRC := $(filter-out src/main.c,$(wildcard src/*.c))
MAIN_OBJ := $(patsubst %.c,$(OBJ_DIR)/%.o,$(MAIN_SRC))
LIB_OBJS := $(patsubst %.c,$(OBJ_DIR)/%.o,$(LIB_SRC))
APP_OBJS := $(MAIN_OBJ) $(LIB_OBJS)

TEST_SRC := $(wildcard tests/*.cpp) $(wildcard tests/*.c)
TEST_OBJS := $(patsubst tests/%.cpp,$(OBJ_DIR)/tests_%.o,$(TEST_SRC))
MINIGTEST_SRC := $(wildcard third_party/minigtest/*.cpp)
MINIGTEST_OBJS := $(patsubst third_party/minigtest/%.cpp,$(OBJ_DIR)/mg_%.o,$(MINIGTEST_SRC))

MKDIR_P = mkdir -p

.PHONY: all build test clean

all: build

build: $(APP) $(TEST_BIN)

$(OBJ_DIR)/%.o: %.c
	@$(MKDIR_P) $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(OBJ_DIR)/%.o: src/%.c
	@$(MKDIR_P) $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(OBJ_DIR)/tests_%.o: tests/%.cpp
	@$(MKDIR_P) $(dir $@)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(OBJ_DIR)/tests_%.o: tests/%.c
	@$(MKDIR_P) $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(OBJ_DIR)/mg_%.o: third_party/minigtest/%.cpp
	@$(MKDIR_P) $(dir $@)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(APP): $(APP_OBJS)
	@$(MKDIR_P) $(BIN_DIR)
	$(CC) $(CFLAGS) $^ -o $@ $(LDFLAGS)

$(TEST_BIN): $(LIB_OBJS) $(TEST_OBJS) $(MINIGTEST_OBJS)
	@$(MKDIR_P) $(TEST_DIR)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

test: build
	@$(MKDIR_P) $(TEST_DIR)
	@echo "Running tests..."
	@$(TEST_BIN) --gtest_output=xml:$(TEST_DIR)/report.xml || true

clean:
	rm -rf $(BUILD_DIR)
