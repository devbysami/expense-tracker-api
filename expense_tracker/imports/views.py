import uuid

from fastapi import BackgroundTasks, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.dependencies import CurrentUser, DBSession
from core.utils import error_response
from imports.models import ImportJob
from imports.utils import MAX_FILE_BYTES, run_import, save_upload


async def start_import(
    file: UploadFile, background: BackgroundTasks, user: CurrentUser, db: DBSession
) -> JSONResponse:
    saved = await save_upload(file)
    if saved is None:
        return error_response(413, f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    path, sha256 = saved

    job = ImportJob(id=uuid.uuid4().hex, user_id=user.id, filename=file.filename or "upload.csv",
                    file_sha256=sha256, status="pending", errors=[])
    db.add(job)
    await db.commit()

    # Runs after the 202 is sent. Simple-over-perfect: see README (production would hand it to the worker).
    background.add_task(run_import, job.id, user.id, path)
    return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)


async def get_import(job_id: str, user: CurrentUser, db: DBSession) -> ImportJob | JSONResponse:
    job = await db.scalar(select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user.id))
    if job is None:
        return error_response(404, "Import job not found")
    return job
