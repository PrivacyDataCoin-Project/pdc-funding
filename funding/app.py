"""FastAPI application — PDC community funding (FFS-style)."""

from __future__ import annotations

import html
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from funding.amounts import format_amount, funding_pct
from funding.config import CATEGORIES, STATUSES, Settings, get_settings
from funding.db import Database, User
from funding.prices import atomic_to_usd, fetch_prices
from funding.scanner import DepositScanner
from funding.service import PROPOSAL_TEMPLATE, FundingService
from funding.wallet import WalletClient

logger = logging.getLogger("pdc-funding")
BASE_DIR = Path(__file__).resolve().parent


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path)
    wallet = WalletClient(settings)
    service = FundingService(settings, db, wallet)
    scanner = DepositScanner(settings, db, wallet)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        service.bootstrap_admin()
        scanner.start()
        yield
        await scanner.stop()

    # Public /api/v1 is read-only; no OpenAPI UI in production surface.
    app = FastAPI(
        title=settings.site_name,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["pdc"] = format_amount
    templates.env.filters["pct"] = lambda received, target: (
        f"{funding_pct(int(received), int(target)):.1f}"
    )

    app.state.settings = settings
    app.state.db = db
    app.state.wallet = wallet
    app.state.service = service
    app.state.scanner = scanner

    def current_user(request: Request) -> Optional[User]:
        raw = request.session.get("user_id")
        if not raw:
            return None
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            request.session.pop("user_id", None)
            return None
        user = db.get_user(uid)
        if user is None:
            request.session.pop("user_id", None)
        return user

    def is_primary_admin(user: Optional[User]) -> bool:
        """Money/status/user admin UI: bootstrap admin account only."""
        if not user or not user.is_admin:
            return False
        expected = (settings.bootstrap_admin_user or "admin").strip().lower()
        return user.username.lower() == expected

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf_token")
        if not isinstance(tok, str) or len(tok) < 32:
            tok = secrets.token_hex(32)
            request.session["csrf_token"] = tok
        return tok

    def require_csrf(request: Request, token: str) -> None:
        expected = request.session.get("csrf_token")
        got = (token or "").strip()
        if (
            not isinstance(expected, str)
            or not expected
            or not got
            or not secrets.compare_digest(expected, got)
        ):
            raise HTTPException(403, "invalid CSRF token")

    def base_ctx(request: Request, **extra: Any) -> dict[str, Any]:
        book = fetch_prices(settings)
        pdc_usd = book.pdc_usd() if book else None
        user = current_user(request)
        ctx = {
            "request": request,
            "site_name": settings.site_name,
            "user": user,
            "is_primary_admin": is_primary_admin(user),
            "csrf_token": csrf_token(request),
            "categories": CATEGORIES,
            "statuses": STATUSES,
            "wallet_enabled": settings.wallet_enabled,
            "pdc_usd": pdc_usd,
            "price_source": book.source if book else None,
            "explorer_tx_url": settings.explorer_tx_url,
            "flash": request.session.pop("flash", None),
        }
        ctx.update(extra)
        return ctx

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.session["flash"] = {"message": message, "kind": kind}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        status_filter: Optional[int] = None,
        cat: Optional[str] = None,
    ):
        proposals = db.list_proposals(status=status_filter, category=cat, limit=100)
        return templates.TemplateResponse(
            request,
            "index.html",
            base_ctx(
                request,
                proposals=proposals,
                status_filter=status_filter,
                cat_filter=cat,
            ),
        )

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        return templates.TemplateResponse(request, "about.html", base_ctx(request))

    @app.get("/register", response_class=HTMLResponse)
    def register_get(request: Request):
        return templates.TemplateResponse(
            request, "auth.html", base_ctx(request, mode="register")
        )

    def _start_enroll(request: Request, user: User) -> RedirectResponse:
        try:
            material = service.begin_totp_enroll(user)
        except Exception as exc:
            flash(request, f"Could not start 2FA setup: {exc}", "error")
            return RedirectResponse("/login", status_code=303)
        request.session.pop("user_id", None)
        request.session["auth_pending"] = {
            "user_id": user.id,
            "purpose": "enroll",
        }
        request.session["totp_enroll"] = {
            "secret_base32": material["secret_base32"],
            "qr_data_url": material["qr_data_url"],
        }
        return RedirectResponse("/account/2fa/enroll", status_code=303)

    @app.post("/register")
    def register_post(
        request: Request,
        username: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
    ):
        try:
            user = service.register(username, email, password)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/register", status_code=303)
        except Exception:
            flash(request, "Could not register (username/email may be taken)", "error")
            return RedirectResponse("/register", status_code=303)
        flash(request, "Account created — set up authenticator 2FA to continue.", "ok")
        return _start_enroll(request, user)

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        return templates.TemplateResponse(
            request, "auth.html", base_ctx(request, mode="login")
        )

    @app.post("/login")
    def login_post(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        user = service.authenticate(username, password)
        if not user:
            flash(request, "Invalid credentials", "error")
            return RedirectResponse("/login", status_code=303)
        request.session.pop("user_id", None)
        if not user.totp_enabled:
            flash(request, "Set up authenticator 2FA to finish signing in.", "ok")
            return _start_enroll(request, user)
        try:
            challenge_id = service.start_login_totp_challenge(user)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/login", status_code=303)
        request.session["auth_pending"] = {
            "user_id": user.id,
            "purpose": "login",
            "challenge_id": challenge_id,
        }
        return RedirectResponse("/login/2fa", status_code=303)

    @app.get("/login/2fa", response_class=HTMLResponse)
    def login_2fa_get(request: Request):
        pending = request.session.get("auth_pending") or {}
        if pending.get("purpose") != "login" or not pending.get("challenge_id"):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "auth_totp.html", base_ctx(request, mode="login")
        )

    @app.post("/login/2fa")
    def login_2fa_post(request: Request, code: str = Form(...)):
        pending = request.session.get("auth_pending") or {}
        challenge_id = pending.get("challenge_id")
        if pending.get("purpose") != "login" or not challenge_id:
            flash(request, "Login session expired — try again.", "error")
            return RedirectResponse("/login", status_code=303)
        try:
            user = service.finish_login_totp(str(challenge_id), code)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/login/2fa", status_code=303)
        request.session.pop("auth_pending", None)
        request.session.pop("totp_enroll", None)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.get("/account/2fa/enroll", response_class=HTMLResponse)
    def enroll_2fa_get(request: Request):
        pending = request.session.get("auth_pending") or {}
        enroll = request.session.get("totp_enroll") or {}
        if pending.get("purpose") != "enroll" or not pending.get("user_id"):
            return RedirectResponse("/login", status_code=303)
        if not enroll.get("qr_data_url"):
            user = db.get_user(int(pending["user_id"]))
            if not user:
                return RedirectResponse("/login", status_code=303)
            return _start_enroll(request, user)
        return templates.TemplateResponse(
            request,
            "auth_enroll.html",
            base_ctx(
                request,
                qr_data_url=enroll.get("qr_data_url"),
                secret_base32=enroll.get("secret_base32"),
            ),
        )

    @app.post("/account/2fa/enroll")
    def enroll_2fa_post(request: Request, code: str = Form(...)):
        pending = request.session.get("auth_pending") or {}
        if pending.get("purpose") != "enroll" or not pending.get("user_id"):
            flash(request, "2FA setup expired — log in again.", "error")
            return RedirectResponse("/login", status_code=303)
        user = db.get_user(int(pending["user_id"]))
        if not user:
            flash(request, "Account missing", "error")
            return RedirectResponse("/register", status_code=303)
        try:
            user = service.confirm_totp_enroll(user, code)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/account/2fa/enroll", status_code=303)
        request.session.pop("auth_pending", None)
        request.session.pop("totp_enroll", None)
        request.session["user_id"] = user.id
        flash(request, "Authenticator enabled. You are signed in.", "ok")
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_get(request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request, "password.html", base_ctx(request)
        )

    @app.post("/account/password")
    def password_post(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        new_password2: str = Form(...),
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if new_password != new_password2:
            flash(request, "New passwords do not match", "error")
            return RedirectResponse("/account/password", status_code=303)
        try:
            service.change_password(user, current_password, new_password)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/account/password", status_code=303)
        flash(request, "Password updated.", "ok")
        return RedirectResponse("/", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not is_primary_admin(user):
            raise HTTPException(403, "Only the admin account can manage users")
        users = db.list_users(limit=500)
        return templates.TemplateResponse(
            request, "admin_users.html", base_ctx(request, users=users)
        )

    @app.post("/admin/users/{user_id}/reset-password")
    def admin_reset_password(
        request: Request, user_id: int, csrf_token: str = Form("")
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not is_primary_admin(user):
            raise HTTPException(403, "Only the admin account can manage users")
        require_csrf(request, csrf_token)
        try:
            new_password = service.admin_reset_password(user, user_id)
            flash(
                request,
                f"Password reset for user #{user_id}. New password (copy now): {new_password}",
                "ok",
            )
        except (PermissionError, KeyError, ValueError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/reset-2fa")
    def admin_reset_2fa(
        request: Request, user_id: int, csrf_token: str = Form("")
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not is_primary_admin(user):
            raise HTTPException(403, "Only the admin account can manage users")
        require_csrf(request, csrf_token)
        try:
            service.admin_reset_totp(user, user_id)
            flash(
                request,
                f"2FA cleared for user #{user_id}. They must enroll again on next login.",
                "ok",
            )
        except (PermissionError, KeyError, ValueError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/proposals/new", response_class=HTMLResponse)
    def proposal_new_get(request: Request):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "proposal_form.html",
            base_ctx(request, default_content=PROPOSAL_TEMPLATE),
        )

    @app.post("/proposals/new")
    def proposal_new_post(
        request: Request,
        headline: str = Form(...),
        category: str = Form(...),
        funds_target: str = Form(...),
        content: str = Form(...),
        payout_address: str = Form(""),
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        try:
            proposal = service.create_proposal(
                user,
                headline=headline,
                content=content,
                category=category,
                funds_target=funds_target,
                payout_address=payout_address,
            )
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/proposals/new", status_code=303)
        flash(request, "Proposal submitted — awaiting review.", "ok")
        return RedirectResponse(f"/proposals/{proposal.id}", status_code=303)

    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    def proposal_detail(request: Request, proposal_id: int):
        proposal = db.get_proposal(proposal_id)
        if not proposal:
            raise HTTPException(404, "Proposal not found")
        if proposal.status >= 2 and not proposal.integrated_address:
            try:
                proposal = service.ensure_donation_address(proposal)
            except Exception as exc:
                logger.warning("donation address: %s", exc)
        progress = service.progress(proposal)
        deposits = db.list_deposits(proposal_id)
        payouts = db.list_payouts(proposal_id)
        comments = db.list_comments(proposal_id)
        book = fetch_prices(settings)
        pdc_usd = book.pdc_usd() if book else None
        return templates.TemplateResponse(
            request,
            "proposal.html",
            base_ctx(
                request,
                proposal=proposal,
                progress=progress,
                deposits=deposits,
                payouts=payouts,
                comments=comments,
                content_html=_markdownish(proposal.content),
                received_usd=atomic_to_usd(progress["received_atomic"], pdc_usd),
                paid_usd=atomic_to_usd(progress["paid_atomic"], pdc_usd),
                available_usd=atomic_to_usd(progress["available_atomic"], pdc_usd),
                target_usd=atomic_to_usd(progress["target_atomic"], pdc_usd),
                transfer_fee_atomic=settings.transfer_fee_atomic,
            ),
        )

    @app.post("/proposals/{proposal_id}/status")
    def proposal_status(
        request: Request,
        proposal_id: int,
        new_status: int = Form(...),
        csrf_token: str = Form(""),
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not is_primary_admin(user):
            raise HTTPException(403, "Only the admin account can change status")
        require_csrf(request, csrf_token)
        try:
            service.set_status(user, proposal_id, new_status)
            flash(request, f"Status → {STATUSES.get(new_status, new_status)}", "ok")
        except PermissionError:
            raise HTTPException(403, "Admin only")
        except (ValueError, KeyError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.post("/proposals/{proposal_id}/comment")
    def proposal_comment(
        request: Request,
        proposal_id: int,
        message: str = Form(...),
        csrf_token: str = Form(""),
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        require_csrf(request, csrf_token)
        try:
            service.add_comment(user, proposal_id, message)
        except (ValueError, KeyError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.post("/proposals/{proposal_id}/payout")
    def proposal_payout(
        request: Request,
        proposal_id: int,
        amount: str = Form(...),
        to_address: str = Form(""),
        milestone_note: str = Form(""),
        csrf_token: str = Form(""),
    ):
        user = current_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if not is_primary_admin(user):
            raise HTTPException(403, "Only the admin account can pay out escrow")
        require_csrf(request, csrf_token)
        try:
            result = service.payout_milestone(
                user,
                proposal_id,
                amount=amount,
                to_address=to_address or None,
                milestone_note=milestone_note,
            )
            flash(
                request,
                f"Payout sent: {format_amount(result['send_atomic'])} PDC "
                f"(escrow −{format_amount(result['amount_atomic'])}, "
                f"fee {format_amount(result['fee_atomic'])}; "
                f"tx {result['tx_hash'][:16]}…)",
                "ok",
            )
        except (PermissionError, ValueError, KeyError, RuntimeError) as exc:
            flash(request, str(exc), "error")
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.get("/api/v1/proposals")
    def api_proposals(
        status_filter: Optional[int] = None,
        cat: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ):
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        items = db.list_proposals(
            status=status_filter, category=cat, limit=limit, offset=offset
        )
        return [
            {
                "id": p.id,
                "headline": p.headline,
                "category": p.category,
                "status": p.status,
                "status_label": STATUSES.get(p.status),
                "author": p.author,
                "funds_target": format_amount(p.funds_target_atomic),
                "funds_target_atomic": p.funds_target_atomic,
                "received_atomic": p.received_atomic,
                "paid_atomic": p.paid_atomic,
                "pending_atomic": p.pending_atomic,
                "available_atomic": max(0, p.received_atomic - p.paid_atomic),
                "funded_pct": funding_pct(p.received_atomic, p.funds_target_atomic),
                "addr_donation": p.integrated_address or None,
                "created_at": p.created_at,
            }
            for p in items
        ]

    @app.get("/api/v1/health")
    def api_health():
        return {
            "ok": True,
            "wallet": "live" if settings.wallet_enabled else "dry-run",
            "site": settings.site_name,
        }

    return app


def _markdownish(text: str) -> str:
    """Minimal safe formatting: escape HTML, paragraphs, #### headings, lists."""
    escaped = html.escape(text or "")
    lines = escaped.splitlines()
    out: list[str] = []
    in_list = False
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + "<br>\n".join(para) + "</p>")
            para = []

    def end_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_para()
            end_list()
            continue
        if stripped.startswith("#### "):
            flush_para()
            end_list()
            out.append(f"<h3>{stripped[5:]}</h3>")
        elif stripped.startswith("- "):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{stripped[2:]}</li>")
        else:
            end_list()
            para.append(stripped)
    flush_para()
    end_list()
    return "\n".join(out)


app = create_app()
