"""FastAPI 路由：提交项目、查状态、查看展示页。"""
from __future__ import annotations

import asyncio
import shutil
import threading
import uuid
import logging
from pathlib import Path
from typing import Optional, Union, List

import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel

from app.core.config import settings
from app.core.session import COOKIE_NAME, sign_session, verify_session
from app.models.models import (
    create_project, get_project, delete_project, update_generated,
    init_db, upsert_user, upsert_card, TaskStatus,
)
from app.tasks import process_project

router = APIRouter()
log = logging.getLogger(__name__)


def _current_user(request: Request) -> Optional[dict]:
    """从 cookie 解出当前登录用户；未登录返回 None。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_session(token)


def _require_user(request: Request) -> dict:
    """提交类接口用：必须登录，否则 401。"""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "请先通过 tForum 登录")
    return user


def _run_in_background(project_id: str) -> None:
    """eager 模式下在后台线程跑生成任务，避免阻塞提交接口。"""
    try:
        process_project.run(project_id)
    except Exception:
        # 任务内部已会 set_failed，这里是兜底
        log.exception("后台生成任务异常: %s", project_id)
        from app.models.models import set_failed
        set_failed(project_id, "后台任务异常")


def _dispatch(project_id: str) -> None:
    """提交立即返回：eager 用后台线程，否则丢给 Celery。"""
    if settings.eager_mode:
        t = threading.Thread(target=_run_in_background, args=(project_id,), daemon=True)
        t.start()
    else:
        process_project.delay(project_id)


class SubmitURL(BaseModel):
    url: str


def _is_allowed_url(url: str) -> bool:
    low = url.lower()
    return any(
        host in low
        for host in ("github.com", "gitlab.com", "gitee.com", "bitbucket.org")
    ) or low.startswith("http")


@router.post("/projects/url")
def submit_url(body: SubmitURL, user: dict = Depends(_require_user)) -> JSONResponse:
    url = body.url.strip()
    if not _is_allowed_url(url):
        raise HTTPException(400, "仅支持 GitLab / Gitee / GitHub 等 git URL")
    project_id = uuid.uuid4().hex[:12]
    create_project(
        project_id, "url", url,
        owner_name=user["username"], owner_id=user["tforum_user_id"],
    )
    _dispatch(project_id)
    return JSONResponse({"project_id": project_id, "status": "pending"})


@router.post("/projects/local")
def submit_local(
    request: Request,
    path: str = Form(...),
    user: dict = Depends(_require_user),
) -> JSONResponse:
    p = Path(path)
    if not p.exists():
        raise HTTPException(400, f"路径不存在: {path}")
    project_id = uuid.uuid4().hex[:12]
    create_project(
        project_id, "local", str(p.resolve()),
        owner_name=user["username"], owner_id=user["tforum_user_id"],
    )
    _dispatch(project_id)
    return JSONResponse({"project_id": project_id, "status": "pending"})


@router.post("/projects/upload")
async def submit_upload(
    file: UploadFile = File(...),
    user: dict = Depends(_require_user),
) -> JSONResponse:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "请上传 .zip 压缩包")
    project_id = uuid.uuid4().hex[:12]
    settings.ensure_dirs()
    zip_path = settings.uploads_dir / f"{project_id}.zip"
    with zip_path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    create_project(
        project_id, "zip", str(zip_path),
        owner_name=user["username"], owner_id=user["tforum_user_id"],
    )
    _dispatch(project_id)
    return JSONResponse({"project_id": project_id, "status": "pending"})


@router.get("/projects/{project_id}/status")
def get_status(project_id: str) -> JSONResponse:
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    return JSONResponse({
        "project_id": project_id,
        "status": proj["status"],
        "progress": proj["progress"],
        "message": proj["message"],
        "error": proj["error"],
    })


@router.get("/projects/{project_id}/status/stream")
async def stream_status(project_id: str):
    """SSE 推送状态变化（轮询 DB）。"""
    from fastapi.responses import StreamingResponse

    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")

    async def event_gen():
        last = None
        for _ in range(600):  # 最多 10 分钟
            proj = get_project(project_id)
            if not proj:
                break
            cur = (proj["status"], proj["progress"])
            if cur != last:
                last = cur
                import json
                data = json.dumps({
                    "status": proj["status"],
                    "progress": proj["progress"],
                    "message": proj["message"],
                    "error": proj["error"],
                }, ensure_ascii=False)
                yield f"data: {data}\n\n"
                if proj["status"] in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                    return
            await asyncio.sleep(1)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


_AUTH_SNIPPET = """
<style>
.pa-fab{position:fixed;right:24px;bottom:24px;z-index:50;background:#f85149;color:#fff;
border:none;padding:11px 20px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;
box-shadow:0 6px 18px rgba(0,0,0,0.45);font-family:inherit;}
.pa-fab:hover{background:#da3633;}
.pa-edit-btn{display:inline-block;background:transparent;border:1px solid #30363d;color:#8b949e;
padding:2px 9px;border-radius:6px;font-size:12px;cursor:pointer;margin-bottom:6px;font-family:inherit;}
.pa-edit-btn:hover{border-color:#58a6ff;color:#58a6ff;}
.pa-edit-ta{width:100%;min-height:90px;background:#0d1117;color:#c9d1d9;border:1px solid #58a6ff;
border-radius:8px;padding:10px;font-family:inherit;font-size:14px;line-height:1.6;box-sizing:border-box;}
.pa-edit-act{margin:6px 0;display:flex;gap:8px;}
.pa-edit-act button{padding:5px 14px;border-radius:6px;font-size:13px;cursor:pointer;
border:1px solid #30363d;background:#161b22;color:#c9d1d9;font-family:inherit;}
.pa-edit-act .sv{background:#58a6ff;color:#fff;border-color:#58a6ff;}
</style>
<button class="pa-fab" onclick="paDelete()">删除项目</button>
<script>
var PA_PID="__PID__";
function paDelete(){
  if(!confirm('确认删除该项目？此操作不可恢复。'))return;
  fetch('/projects/'+PA_PID,{method:'DELETE'}).then(function(r){
    if(r.status===403){alert('无权删除该项目');return;}
    if(r.status===401){alert('请先登录');return;}
    if(!r.ok){alert('删除失败');return;}
    alert('已删除');location.href='/';
  }).catch(function(){alert('网络错误');});
}
function paVal(el,type){
  if(type==='list')return Array.prototype.map.call(el.children,function(c){return c.textContent;}).join('\\n');
  return el.innerText;
}
function paEdit(el){
  var field=el.getAttribute('data-field');
  var type=el.getAttribute('data-type')||'text';
  var ta=document.createElement('textarea');ta.className='pa-edit-ta';ta.value=paVal(el,type);
  var acts=document.createElement('div');acts.className='pa-edit-act';
  var sv=document.createElement('button');sv.className='sv';sv.textContent='保存';
  var cc=document.createElement('button');cc.textContent='取消';
  acts.appendChild(sv);acts.appendChild(cc);
  el.parentNode.insertBefore(acts,el);
  el.parentNode.insertBefore(ta,el);
  el.style.display='none';
  var btn=document.querySelector('.pa-edit-btn[data-for="'+field+'"]');if(btn)btn.style.display='none';
  ta.focus();
  sv.onclick=function(){
    var raw=ta.value;
    var value=type==='list'?raw.split('\\n').map(function(s){return s.trim();}).filter(Boolean):raw;
    fetch('/projects/'+PA_PID,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({field:field,value:value})}).then(function(r){
        if(r.status===403){alert('无权编辑');return;}
        if(r.status===401){alert('请先登录');return;}
        if(!r.ok){alert('保存失败');return;}
        location.reload();
      }).catch(function(){alert('网络错误');});
  };
  cc.onclick=function(){
    ta.remove();acts.remove();el.style.display='';if(btn)btn.style.display='';
  };
}
document.querySelectorAll('[data-field]').forEach(function(el){
  var field=el.getAttribute('data-field');
  var btn=document.createElement('button');
  btn.className='pa-edit-btn';btn.textContent='✎ 编辑';btn.setAttribute('data-for',field);
  btn.onclick=function(){paEdit(el);};
  el.parentNode.insertBefore(btn,el);
});
</script>
"""


def _inject_auth_tools(html: str, project_id: str) -> str:
    snippet = _AUTH_SNIPPET.replace("__PID__", project_id)
    if "</body>" in html:
        return html.replace("</body>", snippet + "</body>", 1)
    return html + snippet


def _can_modify(user: Optional[dict], proj: dict) -> bool:
    """作者本人或管理员可改/可删。"""
    if not user:
        return False
    if proj.get("owner_id") == user["tforum_user_id"]:
        return True
    return user.get("role") == "admin"


@router.get("/projects/{project_id}/page", response_class=HTMLResponse)
def view_page(project_id: str, request: Request) -> HTMLResponse:
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if proj["status"] != TaskStatus.DONE.value:
        raise HTTPException(409, f"项目尚未就绪，当前状态: {proj['status']}")
    html_path = Path(proj["html_path"])
    if not html_path.exists():
        raise HTTPException(404, "展示页文件缺失")
    html = html_path.read_text(encoding="utf-8")
    # 老页面没有 data-field 标记，按存储的 JSON 重新渲染一次升级
    if "data-field" not in html and proj.get("generated_json") and proj.get("parsed_json"):
        try:
            import json as _json
            from app.llm.renderer import render_page
            html = render_page(_json.loads(proj["parsed_json"]), _json.loads(proj["generated_json"]))
            html_path.write_text(html, encoding="utf-8")
        except Exception:
            log.warning("老页面升级失败: %s", project_id)
    if _can_modify(_current_user(request), proj):
        html = _inject_auth_tools(html, project_id)
    return HTMLResponse(html)


@router.delete("/projects/{project_id}")
def remove_project(project_id: str, request: Request) -> JSONResponse:
    """删除项目：仅作者或管理员。鉴权通过后清 DB 行 + 生成页 + repo/upload。"""
    user = _require_user(request)
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not _can_modify(user, proj):
        raise HTTPException(403, "无权删除该项目")

    delete_project(project_id)

    if proj.get("html_path"):
        try:
            Path(proj["html_path"]).unlink()
        except Exception:
            log.warning("删除展示页失败: %s", proj.get("html_path"))
    repo_dir = settings.repos_dir / project_id
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    zip_path = settings.uploads_dir / f"{project_id}.zip"
    if zip_path.exists():
        try:
            zip_path.unlink()
        except Exception:
            pass
    log.info("项目 %s 已被 %s 删除", project_id, user.get("username"))
    return JSONResponse({"ok": True})


EDITABLE_FIELDS = {
    "title", "one_line_summary", "architecture_overview", "getting_started",
    "highlights", "use_cases", "tags", "tech_stack",
}
_LIST_FIELDS = {"highlights", "use_cases", "tags", "tech_stack"}


class EditField(BaseModel):
    field: str
    value: Union[str, List[str]]


@router.put("/projects/{project_id}")
def edit_project(project_id: str, body: EditField, request: Request) -> JSONResponse:
    """编辑项目某字段：仅作者或管理员。写回 generated_json，刷新卡片摘要，重渲染静态页。"""
    user = _require_user(request)
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not _can_modify(user, proj):
        raise HTTPException(403, "无权编辑该项目")
    if body.field not in EDITABLE_FIELDS:
        raise HTTPException(400, f"不支持编辑的字段: {body.field}")

    if body.field in _LIST_FIELDS:
        if not isinstance(body.value, list):
            raise HTTPException(400, "该字段应为列表")
        value = [str(x).strip() for x in body.value if str(x).strip()]
    else:
        if not isinstance(body.value, str):
            raise HTTPException(400, "该字段应为文本")
        value = body.value

    import json as _json
    from app.llm.renderer import render_page
    parsed = _json.loads(proj["parsed_json"] or "{}")
    gen = _json.loads(proj["generated_json"] or "{}")
    gen[body.field] = value
    update_generated(project_id, gen)
    upsert_card(project_id, parsed, gen,
                owner_name=proj["owner_name"], owner_id=proj["owner_id"])
    html = render_page(parsed, gen)
    Path(proj["html_path"]).write_text(html, encoding="utf-8")
    log.info("项目 %s 的 %s 被 %s 编辑", project_id, body.field, user.get("username"))
    return JSONResponse({"ok": True})


@router.get("/projects/{project_id}/progress", response_class=HTMLResponse)
def progress_page(project_id: str) -> HTMLResponse:
    """生成进度页：SSE 监听状态，done 后跳展示页。"""
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    from app.llm.renderer import render_template
    html = render_template("progress.html", project_id=project_id)
    return HTMLResponse(html)


@router.get("/projects")
def list_projects(
    page: int = 1,
    per_page: int = 24,
    lang: Optional[str] = None,
    tag: Optional[str] = None,
) -> JSONResponse:
    """项目列表 JSON API：分页 + 语言/标签筛选。"""
    from app.models.models import list_cards
    cards, total = list_cards(page=page, per_page=per_page, lang=lang, tag=tag)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse({
        "cards": cards,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    page: int = 1,
    lang: Optional[str] = None,
    tag: Optional[str] = None,
) -> HTMLResponse:
    """社区首页 = 最新发布列表页。"""
    from app.models.models import list_cards, distinct_filter_values
    from app.llm.renderer import render_template

    per_page = 24
    cards, total = list_cards(page=page, per_page=per_page, lang=lang, tag=tag)
    total_pages = max(1, (total + per_page - 1) // per_page)

    # 分页页码窗口（最多显示 7 个）
    start = max(1, page - 3)
    end = min(total_pages, start + 6)
    start = max(1, end - 6)
    page_range = list(range(start, end + 1))

    user = _current_user(request)
    html = render_template(
        "list.html",
        cards=cards,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        page_range=page_range,
        lang=lang,
        tag=tag,
        langs=distinct_filter_values("lang"),
        tags=distinct_filter_values("tag"),
        current_user=user,
    )
    return HTMLResponse(html)


@router.get("/sso")
def sso_entry(token: Optional[str] = None):
    """tForum 外链跳转入口：服务端校验 token → 建会话 → 回首页。

    tForum 管理后台把外部栏目 URL 配成 {PROJECTAGENT_PUBLIC_URL}/sso?token={token}，
    用户点击后 tForum 前端用 window.open 打开最终 URL，本路由拿到 token 去问 tForum 校验。
    """
    if not token:
        return _sso_fail_page("缺少登录凭证，请从 tForum 站内入口进入。")

    verify_url = f"{settings.tforum_base_url.rstrip('/')}/api/v1/user/verifyToken"
    try:
        resp = httpx.get(verify_url, params={"token": token}, timeout=10.0)
        data = resp.json()
    except Exception as e:
        log.warning("调用 tForum verifyToken 失败: %s", e)
        return _sso_fail_page("无法连接登录服务，请稍后重试。")

    if data.get("code") != 0 or not data.get("data"):
        msg = data.get("message") or "token 无效"
        return _sso_fail_page(f"登录校验失败：{msg}")

    info = data["data"]
    user = upsert_user(info)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=sign_session(user["tforum_user_id"]),
        max_age=settings.sso_session_ttl,
        httponly=True,
        samesite="lax",
        secure=False,  # 本地 http；生产部署 https 时改 True
    )
    return response


def _sso_fail_page(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
        <title>登录失败 · ProjectAgent</title>
        <body style="background:#0d1117;color:#c9d1d9;font-family:sans-serif;
        display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;">
        <div style="text-align:center;max-width:420px;padding:32px;
        background:#161b22;border:1px solid #30363d;border-radius:12px;">
        <h2 style="color:#f85149;margin:0 0 12px;">登录失败</h2>
        <p style="color:#8b949e;margin:0 0 20px;">{msg}</p>
        <a href="/" style="color:#58a6ff;">返回首页</a>
        </div></body></html>""",
        status_code=200,
    )


@router.get("/me")
def me(request: Request) -> JSONResponse:
    """前端探测登录态：返回当前用户或 null。"""
    user = _current_user(request)
    if not user:
        return JSONResponse({"user": None})
    return JSONResponse({"user": {
        "id": user["tforum_user_id"],
        "username": user["username"],
        "avatar": user["avatar"],
        "role": user["role"],
    }})


@router.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})
