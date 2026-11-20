# Embedding benchmark (2026-11-20)

Dataset `embedding` v1: 81 synthetic SRE documents, 85 questions with graded relevance.
Measured on arm64 (macOS-27.0.1-arm64-arm-64bit), torch threads per model: 4. Latency and throughput are relative to this machine; quality numbers are not.

## Quality (hybrid = keyword + exact identifiers + vector, fused as search does)

| Model | Dims | nDCG@10 hybrid | nDCG@10 vector | Recall@5 hybrid | MRR@10 hybrid | Answer@1 hybrid | Separation AUC | Margin |
|---|---|---|---|---|---|---|---|---|
| keyword only (no model) | - | 0.885 | - | 0.859 | 0.939 | 0.776 | - | - |
| all-MiniLM-L6-v2 | 384 | 0.921 | 0.890 | 0.893 | 0.961 | 0.835 | 0.971 | 0.076 |
| bge-small-en-v1.5 | 384 | 0.921 | 0.896 | 0.875 | 0.972 | 0.835 | 0.961 | 0.070 |
| bge-small-en-v1.5 (no instruction) | 384 | 0.924 | 0.883 | 0.877 | 0.978 | 0.824 | 0.959 | 0.069 |
| bge-base-en-v1.5 | 768 | 0.920 | 0.915 | 0.885 | 0.965 | 0.847 | 0.970 | 0.105 |
| bge-base-en-v1.5 (no instruction) | 768 | 0.920 | 0.887 | 0.879 | 0.965 | 0.835 | 0.965 | 0.105 |
| e5-base-v2 | 768 | 0.919 | 0.902 | 0.880 | 0.978 | 0.824 | 0.964 | 0.037 |
| e5-base-v2 (no prefixes) | 768 | 0.922 | 0.911 | 0.882 | 0.972 | 0.835 | 0.965 | 0.033 |
| bge-m3 (serving today) | 1024 | 0.923 | 0.927 | 0.879 | 0.972 | 0.835 | 0.961 | 0.098 |

## Cost to run

| Model | Load s | Query p50 ms | Query p95 ms | Chunks/s (1300 chars) | CPU s/chunk | Peak RSS MB | Bytes/vector |
|---|---|---|---|---|---|---|---|
| all-MiniLM-L6-v2 | 2.7 | 3.1 | 3.5 | 120.12 | 0.0158 | 818.8 | 1544 |
| bge-small-en-v1.5 | 1.97 | 6.0 | 6.5 | 32.99 | 0.0691 | 911.9 | 1544 |
| bge-small-en-v1.5 (no instruction) | 1.97 | 6.3 | 7.2 | 31.86 | 0.0711 | 863.4 | 1544 |
| bge-base-en-v1.5 | 2.02 | 16.4 | 17.2 | 12.66 | 0.1334 | 1722.5 | 3080 |
| bge-base-en-v1.5 (no instruction) | 2.64 | 16.2 | 17.5 | 13.01 | 0.1306 | 1441.2 | 3080 |
| e5-base-v2 | 2.64 | 15.5 | 17.4 | 12.58 | 0.1351 | 1829.6 | 3080 |
| e5-base-v2 (no prefixes) | 2.54 | 15.8 | 16.6 | 12.86 | 0.1324 | 1570.2 | 3080 |
| bge-m3 (serving today) | 3.6 | 53.7 | 56.3 | 3.64 | 0.4512 | 2734.1 | 4104 |

## nDCG@10 (hybrid) by category

| Model | aws | code | incidents | kubernetes | loki | opentelemetry | prometheus | runbooks | terraform |
|---|---|---|---|---|---|---|---|---|---|
| keyword only | 0.903 | 0.949 | 0.932 | 0.807 | 0.929 | 0.877 | 0.858 | 0.871 | 0.891 |
| all-MiniLM-L6-v2 | 0.926 | 0.958 | 0.938 | 0.916 | 0.986 | 0.928 | 0.883 | 0.882 | 0.896 |
| bge-small-en-v1.5 | 0.904 | 0.963 | 0.932 | 0.899 | 0.978 | 0.933 | 0.913 | 0.880 | 0.913 |
| bge-small-en-v1.5 (no instruction) | 0.917 | 0.962 | 0.932 | 0.899 | 0.955 | 0.979 | 0.904 | 0.876 | 0.918 |
| bge-base-en-v1.5 | 0.918 | 0.954 | 0.938 | 0.899 | 0.965 | 0.931 | 0.907 | 0.901 | 0.892 |
| bge-base-en-v1.5 (no instruction) | 0.919 | 0.962 | 0.935 | 0.890 | 0.963 | 0.936 | 0.904 | 0.896 | 0.899 |
| e5-base-v2 | 0.935 | 0.958 | 0.932 | 0.855 | 0.969 | 0.971 | 0.889 | 0.879 | 0.925 |
| e5-base-v2 (no prefixes) | 0.943 | 0.958 | 0.932 | 0.865 | 0.967 | 0.971 | 0.891 | 0.882 | 0.926 |
| bge-m3 (serving today) | 0.916 | 0.973 | 0.935 | 0.893 | 0.984 | 0.932 | 0.916 | 0.875 | 0.903 |

## nDCG@10 (hybrid) by question type

| Model | code | conceptual | how_to | identifier | incident_history | troubleshooting |
|---|---|---|---|---|---|---|
| keyword only | 0.952 | 0.909 | 0.909 | 0.948 | 0.919 | 0.813 |
| all-MiniLM-L6-v2 | 0.962 | 0.994 | 0.880 | 0.977 | 0.925 | 0.890 |
| bge-small-en-v1.5 | 0.968 | 0.982 | 0.907 | 0.979 | 0.919 | 0.877 |
| bge-small-en-v1.5 (no instruction) | 0.967 | 0.979 | 0.925 | 0.981 | 0.919 | 0.876 |
| bge-base-en-v1.5 | 0.958 | 0.989 | 0.898 | 0.980 | 0.926 | 0.878 |
| bge-base-en-v1.5 (no instruction) | 0.967 | 0.989 | 0.901 | 0.974 | 0.922 | 0.878 |
| e5-base-v2 | 0.962 | 0.997 | 0.932 | 0.969 | 0.919 | 0.865 |
| e5-base-v2 (no prefixes) | 0.962 | 0.994 | 0.932 | 0.970 | 0.919 | 0.873 |
| bge-m3 (serving today) | 0.979 | 0.994 | 0.895 | 0.976 | 0.922 | 0.883 |

## Is the difference real? (paired bootstrap over questions, 10000 samples, nDCG@10, 95% interval; * = significant)

| Model | hybrid vs keyword only | hybrid vs bge-m3 (serving today) | vector vs bge-m3 (serving today) |
|---|---|---|---|
| all-MiniLM-L6-v2 | +0.036 [+0.015, +0.059] * | -0.002 [-0.017, +0.013] | -0.037 [-0.071, -0.005] * |
| bge-small-en-v1.5 | +0.036 [+0.017, +0.059] * | -0.001 [-0.011, +0.010] | -0.031 [-0.068, +0.002] |
| bge-small-en-v1.5 (no instruction) | +0.039 [+0.019, +0.062] * | +0.002 [-0.012, +0.017] | -0.045 [-0.084, -0.009] * |
| bge-base-en-v1.5 | +0.035 [+0.014, +0.059] * | -0.003 [-0.016, +0.011] | -0.013 [-0.043, +0.017] |
| bge-base-en-v1.5 (no instruction) | +0.035 [+0.014, +0.058] * | -0.003 [-0.015, +0.010] | -0.041 [-0.079, -0.004] * |
| e5-base-v2 | +0.034 [+0.017, +0.054] * | -0.004 [-0.022, +0.016] | -0.025 [-0.056, +0.004] |
| e5-base-v2 (no prefixes) | +0.037 [+0.019, +0.057] * | -0.000 [-0.018, +0.018] | -0.016 [-0.046, +0.012] |
| bge-m3 (serving today) | +0.037 [+0.015, +0.062] * | - | - |

## Query expansion (nDCG@10)

| Model | hybrid | hybrid + expansion |
|---|---|---|
| keyword only | 0.885 | 0.896 |
| all-MiniLM-L6-v2 | 0.921 | 0.933 |
| bge-small-en-v1.5 | 0.921 | 0.933 |
| bge-small-en-v1.5 (no instruction) | 0.924 | 0.934 |
| bge-base-en-v1.5 | 0.920 | 0.931 |
| bge-base-en-v1.5 (no instruction) | 0.920 | 0.931 |
| e5-base-v2 | 0.919 | 0.932 |
| e5-base-v2 (no prefixes) | 0.922 | 0.936 |
| bge-m3 (serving today) | 0.923 | 0.931 |
