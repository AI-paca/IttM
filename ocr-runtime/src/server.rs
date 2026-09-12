use crate::{
    document::Limits,
    pipeline::{self, Options},
};
use anyhow::{Context, Result};
use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Multipart, Query, State},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    io::Write,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context as TaskContext, Poll},
    time::Duration,
};
use tokio::sync::{Semaphore, mpsc};
use tokio_stream::{Stream, wrappers::ReceiverStream};
use tower_http::cors::{AllowOrigin, CorsLayer};

struct AppState {
    limits: Limits,
    permits: Arc<Semaphore>,
}
type Shared = Arc<AppState>;
struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}
fn bad(error: impl std::fmt::Display) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, error.to_string())
}
fn internal(error: impl std::fmt::Display) -> ApiError {
    ApiError(StatusCode::INTERNAL_SERVER_ERROR, error.to_string())
}

pub fn router(limits: Limits, concurrency: usize) -> Result<Router> {
    anyhow::ensure!(concurrency > 0, "OCR concurrency must be positive");
    let body_limit = if limits.upload_bytes == 0 {
        DefaultBodyLimit::disable()
    } else {
        DefaultBodyLimit::max(usize::try_from(
            limits.upload_bytes.saturating_add(1024 * 1024),
        )?)
    };
    let mut router = Router::new()
        .route("/health", get(health))
        .route("/v1/health", get(health))
        .route("/readiness", get(readiness))
        .route("/v1/readiness", get(readiness))
        .route("/v1/capabilities", get(capabilities))
        .route("/diagnostics", get(diagnostics))
        .route("/v1/diagnostics", get(diagnostics))
        .route("/pipeline/flags", get(flags))
        .route("/v1/pipeline/flags", get(flags))
        .route("/convert", post(convert))
        .route("/v1/convert", post(convert))
        .route("/convert/stream", post(convert_stream))
        .route("/v1/convert/stream", post(convert_stream))
        .route("/probe", post(probe))
        .route("/v1/probe", post(probe))
        .route("/install-easyocr", post(install_easyocr))
        .layer(body_limit)
        .with_state(Arc::new(AppState {
            limits,
            permits: Arc::new(Semaphore::new(concurrency)),
        }));
    let origins = std::env::var("OCR_CORS_ORIGINS").unwrap_or_default();
    if !origins.is_empty() {
        let origins = origins
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|origin| {
                anyhow::ensure!(origin != "*", "OCR_CORS_ORIGINS must list explicit origins");
                Ok(origin
                    .trim_end_matches('/')
                    .parse::<header::HeaderValue>()?)
            })
            .collect::<Result<Vec<_>>>()?;
        router = router.layer(
            CorsLayer::new()
                .allow_origin(AllowOrigin::list(origins))
                .allow_methods(tower_http::cors::Any)
                .allow_headers(tower_http::cors::Any),
        );
    }
    Ok(router)
}
async fn health() -> Json<Value> {
    Json(json!({"ok":true,"service":"Rust OCR Service"}))
}
fn executable(name: &str) -> bool {
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .any(|dir| dir.join(name).is_file())
}
fn readiness_report() -> Value {
    let checks = json!({"tesseract":executable("tesseract"),"pdftoppm":executable("pdftoppm"),"pdftotext":executable("pdftotext"),"pdfinfo":executable("pdfinfo"),"pipeline_core_abi6":ittm_pipeline_core::ABI_VERSION==6});
    json!({"ready":checks.as_object().unwrap().values().all(|v|v==true),"checks":checks})
}
async fn readiness() -> Response {
    let report = readiness_report();
    let status = if report["ready"] == true {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (status, Json(report)).into_response()
}
async fn capabilities() -> Json<Value> {
    Json(
        json!({"engines":[{"engine":"tesseract","available":executable("tesseract")},{"engine":"easyocr","available":std::env::var_os("ITTM_EASYOCR_PYTHON").is_some()}]}),
    )
}
async fn diagnostics() -> Json<Value> {
    let memory = std::fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let kb = |key: &str| -> f64 {
        memory
            .lines()
            .find_map(|line| {
                let rest = line.strip_prefix(key)?;
                rest.split_whitespace().next()?.parse::<f64>().ok()
            })
            .unwrap_or(0.0)
    };
    let total = kb("MemTotal:");
    let used = (total - kb("MemAvailable:")).max(0.0);
    let gb = |value: f64| (value / 1024.0 / 1024.0 * 100.0).round() / 100.0;
    Json(
        json!({"runtime":"rust","runtime_version":env!("CARGO_PKG_VERSION"),"system":std::env::consts::OS,
        "cpu_cores":std::thread::available_parallelism().map(|n|n.get()).unwrap_or(1),"gpus":[],
        "easyocr_available":std::env::var_os("ITTM_EASYOCR_PYTHON").is_some(),
        "memory_total_gb":gb(total),"memory_used_gb":gb(used)}),
    )
}
async fn flags() -> Json<Value> {
    Json(pipeline::catalog()["api"].clone())
}
async fn install_easyocr() -> ApiError {
    ApiError(
        StatusCode::CONFLICT,
        "EasyOCR uses an optional Python worker. Configure ITTM_EASYOCR_PYTHON on the server."
            .into(),
    )
}
async fn probe() -> Json<Value> {
    let ready = readiness_report();
    Json(
        json!({"ok":ready["ready"],"cases":[{"name":"native_runtime","ok":ready["ready"],"message":"Native OCR/PDF dependencies","elapsed_ms":0}]}),
    )
}

async fn upload(
    mut multipart: Multipart,
    limits: &Limits,
) -> std::result::Result<(tempfile::NamedTempFile, String), ApiError> {
    let mut file = None;
    let mut total = 0_u64;
    while let Some(mut field) = multipart.next_field().await.map_err(bad)? {
        if field.name() != Some("file") {
            continue;
        }
        if file.is_some() {
            return Err(bad("Expected one uploaded file"));
        }
        let name = field.file_name().unwrap_or("upload").to_owned();
        let mut temporary = tempfile::NamedTempFile::new().map_err(internal)?;
        while let Some(chunk) = field
            .chunk()
            .await
            .map_err(|e| ApiError(e.status(), e.body_text()))?
        {
            total = total.saturating_add(chunk.len() as u64);
            if limits.upload_bytes > 0 && total > limits.upload_bytes {
                return Err(ApiError(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "File exceeds upload limit".into(),
                ));
            }
            temporary.write_all(&chunk).map_err(internal)?;
        }
        if total == 0 {
            return Err(bad("Uploaded file is empty"));
        }
        file = Some((temporary, name));
    }
    file.ok_or_else(|| bad("Missing file field"))
}
struct CancelOnDrop(Arc<AtomicBool>);
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}
async fn convert(
    State(state): State<Shared>,
    Query(options): Query<Options>,
    multipart: Multipart,
) -> std::result::Result<Json<pipeline::Conversion>, ApiError> {
    options.profile().map_err(bad)?;
    let permit = state
        .permits
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError(StatusCode::TOO_MANY_REQUESTS, "OCR worker is busy".into()))?;
    let (file, name) = upload(multipart, &state.limits).await?;
    let cancel = Arc::new(AtomicBool::new(false));
    let _guard = CancelOnDrop(cancel.clone());
    let result = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        pipeline::convert_file(file.path(), &name, &options, &state.limits, cancel, |_| {
            Ok(())
        })
    })
    .await
    .map_err(internal)?
    .map_err(bad)?;
    Ok(Json(result))
}
struct ConversionStream {
    receiver: ReceiverStream<std::result::Result<Bytes, Infallible>>,
    heartbeat: tokio::time::Interval,
    _cancel: CancelOnDrop,
}
impl Stream for ConversionStream {
    type Item = std::result::Result<Bytes, Infallible>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<Option<Self::Item>> {
        match Pin::new(&mut self.receiver).poll_next(cx) {
            Poll::Ready(value) => Poll::Ready(value),
            Poll::Pending => {
                if self.heartbeat.poll_tick(cx).is_ready() {
                    Poll::Ready(Some(Ok(Bytes::from_static(
                        b"{\"type\":\"progress\",\"stage\":\"ocr\"}\n",
                    ))))
                } else {
                    Poll::Pending
                }
            }
        }
    }
}
async fn convert_stream(
    State(state): State<Shared>,
    Query(options): Query<Options>,
    multipart: Multipart,
) -> std::result::Result<Response, ApiError> {
    options.profile().map_err(bad)?;
    let permit = state
        .permits
        .clone()
        .try_acquire_owned()
        .map_err(|_| ApiError(StatusCode::TOO_MANY_REQUESTS, "OCR worker is busy".into()))?;
    let (file, name) = upload(multipart, &state.limits).await?;
    let cancel = Arc::new(AtomicBool::new(false));
    let worker_cancel = cancel.clone();
    let (sender, receiver) = mpsc::channel(1);
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let emit = |value: Value| -> Result<()> {
            let mut bytes = serde_json::to_vec(&value)?;
            bytes.push(b'\n');
            sender
                .blocking_send(Ok(Bytes::from(bytes)))
                .context("Stream disconnected")
        };
        if let Err(error) = pipeline::convert_file(
            file.path(),
            &name,
            &options,
            &state.limits,
            worker_cancel,
            emit,
        ) {
            let _ = emit(json!({"type":"error","detail":error.to_string()}));
        }
    });
    let heartbeat = tokio::time::interval_at(
        tokio::time::Instant::now() + Duration::from_secs(15),
        Duration::from_secs(15),
    );
    let stream = ConversionStream {
        receiver: ReceiverStream::new(receiver),
        heartbeat,
        _cancel: CancelOnDrop(cancel),
    };
    Ok((
        [
            (header::CONTENT_TYPE, "application/x-ndjson"),
            (header::CACHE_CONTROL, "no-cache"),
        ],
        Body::from_stream(stream),
    )
        .into_response())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;
    #[tokio::test]
    async fn preserves_health_and_rejects_bad_uploads_and_options() {
        let app = router(
            Limits {
                upload_bytes: 4,
                ..Limits::default()
            },
            1,
        )
        .unwrap();
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(serde_json::from_slice::<Value>(&body).unwrap()["ok"], true);
        let multipart = "--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.png\"\r\n\r\n12345\r\n--b--\r\n";
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/convert")
                    .header(header::CONTENT_TYPE, "multipart/form-data; boundary=b")
                    .body(Body::from(multipart))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/convert?pdf_mode=wrong")
                    .header(header::CONTENT_TYPE, "multipart/form-data; boundary=b")
                    .body(Body::from(multipart))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}
