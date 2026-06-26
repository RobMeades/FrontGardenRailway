/*
 * Copyright 2026 Rob Meades
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef _FGR_HEAP_WRAPPER_H_
#define _FGR_HEAP_WRAPPER_H_

/** @file
 * @brief Heap monitoring for a node of the front garden railway:
 * this MUST be the last header file included in your source file,
 * required in order to be sure no calls to the native malloc()
 * and free() get in, only to the macro versions MALLOC() and FREE().
 */

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------
 * COMPILE-TIME MACROS
 * -------------------------------------------------------------- */

// Remove the standard malloc and free calls
#undef malloc
#undef free
#undef calloc
#undef realloc

// Define macros to get them back
#define FGR_HEAP_REAL_MALLOC malloc
#define FGR_HEAP_REAL_CALLOC calloc
#define FGR_HEAP_REAL_REALLOC realloc
#define FGR_HEAP_REAL_FREE free

// Poison the standard functions
#pragma GCC poison malloc free calloc realloc

// Define the macros that replace them
#define MALLOC(size)         fgr_heap_malloc(size, __FILE__, __LINE__)
#define CALLOC(n, size)      fgr_heap_calloc(n, size, __FILE__, __LINE__)
#define REALLOC(ptr, size)   fgr_heap_realloc(ptr, size, __FILE__, __LINE__)
#define FREE(ptr)            fgr_heap_free(ptr, __FILE__, __LINE__)

/* ----------------------------------------------------------------
 * TYPES
 * -------------------------------------------------------------- */

/* ----------------------------------------------------------------
 * FUNCTIONS
 * -------------------------------------------------------------- */

/** fgr_heap version of malloc(); use MALLOC() to call this function.
 *
 * @param size the number of bytes of memory to allocate.
 * @param path pointer to a null terminated string that is
 *             the path of the source file doing the allocation.
 * @param line the line number in the source file doing the allocation.
 * @return     on auccess a pointer to at least size bytes of
 *             allocated memory, else NULL.
 */
void *fgr_heap_malloc(size_t size, const char *path, int line);

/** fgr_heap version of calloc(); use CALLOC() to call this function.
 *
 * @param n    the number of blocks of memory to allocate.
 * @param size the number of bytes of memory to allocate in each block.
 * @param path pointer to a null terminated string that is
 *             the path of the source file doing the allocation.
 * @param line the line number in the source file doing the allocation.
 * @return     on auccess a pointer to the first block allocated,
 *             else NULL.
 */
void *fgr_heap_calloc(size_t n, size_t size, const char *path, int line);

/** fgr_heap version of realloc(); use REALLOC() to call this function.
 *
 * @param ptr  the address of the memory to reallocate.
 * @param size the number of bytes of memory to reallocate at that address.
 * @param path pointer to a null terminated string that is
 *             the path of the source file doing the reallocation.
 * @param line the line number in the source file doing the reallocation.
 * @return     on auccess a pointer to at least size bytes of
 *             zeroed allocated memory, else NULL.
 */
void *fgr_heap_realloc(void *ptr, size_t size, const char *path, int line);

/** fgr_heap version of free(), use FREE() to call this function.
 *
 * @param ptr  the address of the memory to free.
 * @param path pointer to a null terminated string that is
 *             the path of the source file doing the free.
 * @param line the line number in the source file doing the free.
 */
void fgr_heap_free(void *ptr, const char *path, int line);

#ifdef __cplusplus
}
#endif

/** @}*/

#endif // _FGR_HEAP_WRAPPER_H_

// End of file
