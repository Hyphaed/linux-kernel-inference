// Tensor-core GEMM issue rate through cuBLASLt, one JSON line per mode.
//
// Independent reimplementation of the measurement the Blackwell unlock
// research describes (FP16 with FP16 accumulate vs FP32 accumulate), written
// from the public cuBLASLt API. Nothing here touches firmware or driver state.
//
//   gpu_mma [M=N=K=8192] [iters=40] [repeats=5]
//
// Build (done by gpu_baseline.py):
//   nvcc -O3 -arch=sm_120 -Xcompiler -O3,-march=native gpu_mma.cu -lcublasLt -o gpu_mma
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_fp16.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define CK(x) do { cudaError_t e_ = (x); if (e_) { fprintf(stderr, "CUDA %s:%d %s\n", __FILE__, __LINE__, cudaGetErrorString(e_)); exit(2); } } while (0)
#define CB(x) do { cublasStatus_t s_ = (x); if (s_) { fprintf(stderr, "cuBLAS %s:%d status %d\n", __FILE__, __LINE__, (int)s_); exit(3); } } while (0)

// Random data, not zeros: zeros draw less power and bias the boost clock upward.
template <typename T>
__global__ void fill(T* p, size_t n, unsigned seed) {
    size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x;
    if (i >= n) return;
    unsigned x = (unsigned)i * 2654435761u ^ seed;
    x ^= x >> 13; x *= 0x5bd1e995u; x ^= x >> 15;
    p[i] = (T)(((x & 0xffff) / 65535.0f) - 0.5f);
}

struct Mode {
    const char* name;
    cudaDataType ab, cd;
    cublasComputeType_t comp;
    cudaDataType scale;
};

static void run_mode(cublasLtHandle_t lt, const Mode& m, int n, int iters, int repeats) {
    const size_t elems = (size_t)n * n;
    const size_t esz = 2;   // both fp16 and bf16
    void *A, *B, *C, *ws;
    const size_t wsz = 32u << 20;
    CK(cudaMalloc(&A, elems * esz)); CK(cudaMalloc(&B, elems * esz));
    CK(cudaMalloc(&C, elems * esz)); CK(cudaMalloc(&ws, wsz));
    const int thr = 256, blk = (int)((elems + thr - 1) / thr);
    if (m.ab == CUDA_R_16F) { fill<__half><<<blk, thr>>>((__half*)A, elems, 1); fill<__half><<<blk, thr>>>((__half*)B, elems, 2); }
    else { fill<__nv_bfloat16><<<blk, thr>>>((__nv_bfloat16*)A, elems, 1); fill<__nv_bfloat16><<<blk, thr>>>((__nv_bfloat16*)B, elems, 2); }
    CK(cudaMemset(C, 0, elems * esz));

    cublasLtMatmulDesc_t op;
    CB(cublasLtMatmulDescCreate(&op, m.comp, m.scale));
    cublasLtMatrixLayout_t la, lb, lc;
    CB(cublasLtMatrixLayoutCreate(&la, m.ab, n, n, n));
    CB(cublasLtMatrixLayoutCreate(&lb, m.ab, n, n, n));
    CB(cublasLtMatrixLayoutCreate(&lc, m.cd, n, n, n));
    cublasLtMatmulPreference_t pref;
    CB(cublasLtMatmulPreferenceCreate(&pref));
    CB(cublasLtMatmulPreferenceSetAttribute(pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &wsz, sizeof(wsz)));
    cublasLtMatmulHeuristicResult_t heur;
    int found = 0;
    cublasStatus_t hs = cublasLtMatmulAlgoGetHeuristic(lt, op, la, lb, lc, lc, pref, 1, &heur, &found);
    if (hs || !found) {
        printf("{\"mode\":\"%s\",\"supported\":false,\"status\":%d}\n", m.name, (int)hs);
        fflush(stdout);
        return;
    }

    float a32 = 1.f, b32 = 0.f;
    __half a16 = __float2half(1.f), b16 = __float2half(0.f);
    const void* alpha = m.scale == CUDA_R_16F ? (const void*)&a16 : (const void*)&a32;
    const void* beta  = m.scale == CUDA_R_16F ? (const void*)&b16 : (const void*)&b32;
    cudaStream_t s; CK(cudaStreamCreate(&s));
    cudaEvent_t e0, e1; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
    auto once = [&]() { CB(cublasLtMatmul(lt, op, alpha, A, la, B, lb, beta, C, lc, C, lc, &heur.algo, ws, wsz, s)); };

    for (int i = 0; i < 10; i++) once();            // warm-up, reaches steady clocks
    CK(cudaStreamSynchronize(s));
    std::vector<double> tf;
    for (int r = 0; r < repeats; r++) {
        CK(cudaEventRecord(e0, s));
        for (int i = 0; i < iters; i++) once();
        CK(cudaEventRecord(e1, s));
        CK(cudaEventSynchronize(e1));
        float ms; CK(cudaEventElapsedTime(&ms, e0, e1));
        tf.push_back(2.0 * n * (double)n * n * iters / (ms * 1e-3) / 1e12);
    }
    std::vector<double> sorted = tf; std::sort(sorted.begin(), sorted.end());
    printf("{\"mode\":\"%s\",\"supported\":true,\"n\":%d,\"iters\":%d,\"tflops_best\":%.2f,\"tflops_median\":%.2f,\"reps\":[",
           m.name, n, iters, sorted.back(), sorted[sorted.size() / 2]);
    for (size_t i = 0; i < tf.size(); i++) printf("%s%.2f", i ? "," : "", tf[i]);
    printf("]}\n"); fflush(stdout);

    cublasLtMatmulPreferenceDestroy(pref); cublasLtMatrixLayoutDestroy(la); cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc); cublasLtMatmulDescDestroy(op);
    cudaFree(A); cudaFree(B); cudaFree(C); cudaFree(ws); cudaStreamDestroy(s);
}

int main(int argc, char** argv) {
    int n = argc > 1 ? atoi(argv[1]) : 8192;
    int iters = argc > 2 ? atoi(argv[2]) : 40;
    int repeats = argc > 3 ? atoi(argv[3]) : 5;
    cublasLtHandle_t lt; CB(cublasLtCreate(&lt));
    const Mode modes[] = {
        {"fp16_acc16", CUDA_R_16F,  CUDA_R_16F,  CUBLAS_COMPUTE_16F, CUDA_R_16F},
        {"fp16_acc32", CUDA_R_16F,  CUDA_R_16F,  CUBLAS_COMPUTE_32F, CUDA_R_32F},
        {"bf16_acc32", CUDA_R_16BF, CUDA_R_16BF, CUBLAS_COMPUTE_32F, CUDA_R_32F},
    };
    for (const Mode& m : modes) run_mode(lt, m, n, iters, repeats);
    cublasLtDestroy(lt);
    return 0;
}
