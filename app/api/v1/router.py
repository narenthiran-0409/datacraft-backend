from fastapi import APIRouter

from app.api.v1.ai.routes import router as ai_router
from app.api.v1.approval.routes import router as approval_router
from app.api.v1.auth.routes import router as auth_router
from app.api.v1.connections.routes import router as connections_router
from app.api.v1.data_sources.routes import router as data_sources_router
from app.api.v1.datasets.routes import router as datasets_router
from app.api.v1.jobs.routes import router as jobs_router
from app.api.v1.lineage.routes import router as lineage_router
from app.api.v1.profiling.routes import router as profiling_router
from app.api.v1.publishing.routes import router as publishing_router
from app.api.v1.reports.routes import router as reports_router
from app.api.v1.review.routes import router as review_router
from app.api.v1.rules.routes import router as rules_router
from app.api.v1.staging.routes import router as staging_router
from app.api.v1.users.routes import router as users_router
from app.api.v1.validation.routes import router as validation_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router)
api_v1_router.include_router(users_router)
api_v1_router.include_router(data_sources_router)
api_v1_router.include_router(connections_router)
api_v1_router.include_router(datasets_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(profiling_router)
api_v1_router.include_router(rules_router)
api_v1_router.include_router(validation_router)
api_v1_router.include_router(review_router)
api_v1_router.include_router(approval_router)
api_v1_router.include_router(staging_router)
api_v1_router.include_router(publishing_router)
api_v1_router.include_router(lineage_router)
api_v1_router.include_router(reports_router)
api_v1_router.include_router(ai_router)
