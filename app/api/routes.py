"""FastAPI 路由：提交项目、查状态、查看展示页。"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import uuid
import zipfile
import logging
from pathlib import Path
from typing import Optional, Union, List

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.core.config import settings
from app.core.session import COOKIE_NAME, sign_session, verify_session
from app.models.models import (
    create_project, get_project, delete_project, update_generated,
    init_db, upsert_user, upsert_card, update_status, TaskStatus,
    list_cards, add_favorite, remove_favorite, get_favorite_status,
    set_template_version, set_tforum_token, get_tforum_token,
    get_skilllab_token, set_skilllab_token,
)
from app.tasks import process_project, sync_project

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


def _run_in_background(project_id: str, task=process_project) -> None:
    """eager 模式下在后台线程跑生成任务，避免阻塞提交接口。"""
    try:
        task.run(project_id)
    except Exception:
        # 任务内部已会 set_failed，这里是兜底
        log.exception("后台生成任务异常: %s", project_id)
        from app.models.models import set_failed
        set_failed(project_id, "后台任务异常")


def _dispatch(project_id: str, task=process_project) -> None:
    """提交立即返回：eager 用后台线程，否则丢给 Celery。"""
    if settings.eager_mode:
        t = threading.Thread(target=_run_in_background, args=(project_id, task), daemon=True)
        t.start()
    else:
        task.delay(project_id)


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


class ToolSyncBody(BaseModel):
    """提交工具 API（/api/v1/tools/save）的请求体。"""
    name: str
    type: str
    category: str
    license: str
    difficulty: str
    ai_friendly: bool
    pricing_model: str
    official_url: str = ""
    repo_url: str = ""
    tags: Optional[List[str]] = None
    self_developed: bool = False
    screenshot_url: str = ""
    platforms: Optional[List[str]] = None
    description: str = ""
    scenarios: str = ""
    usage: str = ""
    pros: str = ""
    cons: str = ""
    ai_agent: str = ""
    official_docs: str = ""
    author: str = ""


@router.post("/tools/sync")
def tools_sync(body: ToolSyncBody, user: dict = Depends(_require_user)) -> JSONResponse:
    """代理转发到开源CAX工具库的提交工具接口，地址由 TOOLSYNC_BASE_URL 配置。"""
    if not settings.toolsync_base_url:
        raise HTTPException(400, "工具库同步未配置（TOOLSYNC_BASE_URL 为空）")
    payload = body.model_dump(exclude_none=True)
    if not payload.get("author"):
        payload["author"] = user["username"]
    url = f"{settings.toolsync_base_url.rstrip('/')}/api/v1/tools/save"
    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
        data = resp.json()
    except Exception as e:
        log.warning("调用工具库提交接口失败: %s", e)
        raise HTTPException(502, f"无法连接工具库服务: {e}")
    # 提交成功即触发库数据刷新，首页立即可见
    from app.library import store
    store.trigger_refresh()
    return JSONResponse(data, status_code=resp.status_code)


# ===== SkillLab 技能库同步 =====

class SkillAiFillBody(BaseModel):
    gitUrl: str


class SkillSubmitBody(BaseModel):
    name: str
    description: str
    domain: str
    skillType: str
    tags: List[str] = []
    gitUrl: str
    blogUrl: str = ""
    isOriginal: bool = False
    idempotencyKey: str = ""


def _exchange_skilllab_token(user: dict) -> str:
    """用 tForum token 换 skillLabToken 并缓存（24h 内复用）。"""
    base = settings.skilllab_base_url.rstrip("/")
    tforum_token = get_tforum_token(user["tforum_user_id"])
    if not tforum_token:
        raise HTTPException(
            400, "缺少 tForum 登录凭证，请从 tForum 站内入口重新进入后再同步 Skill",
        )
    try:
        resp = httpx.post(f"{base}/auth/verify", json={"token": tforum_token}, timeout=15.0)
        data = resp.json()
    except Exception as e:
        log.warning("调用 SkillLab auth/verify 失败: %s", e)
        raise HTTPException(502, f"无法连接 SkillLab 服务: {e}")
    if not data.get("ok") or not data.get("skillLabToken"):
        raise HTTPException(
            400, f"SkillLab 登录校验失败：{data.get('message') or 'token 无效'}，请从 tForum 重新进入",
        )
    token = data["skillLabToken"]
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
    set_skilllab_token(user["tforum_user_id"], token, expires_at)
    return token


def _skilllab_token(user: dict) -> str:
    """优先用未过期的缓存 token，否则换取新的。"""
    cached, expires_at = get_skilllab_token(user["tforum_user_id"])
    if cached and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp > datetime.now(timezone.utc) + timedelta(minutes=5):
                return cached
        except ValueError:
            pass
    return _exchange_skilllab_token(user)


def _skilllab_request(
    method: str, path: str, user: dict,
    timeout: float = 30.0, headers: dict | None = None, **kw,
) -> httpx.Response:
    """SkillLab 请求统一入口：X-SkillLab-User-Key（本地）或 Bearer token。

    token 可能因 SkillLab 重启失效（只存服务进程内），收到 401 时清缓存
    重新换取并重试一次。
    """
    base = settings.skilllab_base_url.rstrip("/")
    extra = dict(headers or {})
    if settings.skilllab_user_key:
        extra.setdefault("X-SkillLab-User-Key", settings.skilllab_user_key)
        return httpx.request(method, f"{base}{path}", headers=extra, timeout=timeout, **kw)

    resp = None
    for _ in range(2):
        token = _skilllab_token(user)
        h = dict(extra)
        h["Authorization"] = f"Bearer {token}"
        resp = httpx.request(method, f"{base}{path}", headers=h, timeout=timeout, **kw)
        if resp.status_code != 401:
            return resp
        set_skilllab_token(user["tforum_user_id"], "", None)
    return resp


@router.post("/skills/ai-fill")
def skill_ai_fill(body: SkillAiFillBody, user: dict = Depends(_require_user)) -> JSONResponse:
    """代理 SkillLab AI 填写：传 Git 地址，返回建议的表单字段。"""
    if not settings.skilllab_base_url:
        raise HTTPException(400, "SkillLab 同步未配置（SKILLLAB_BASE_URL 为空）")
    try:
        resp = _skilllab_request(
            "POST", "/skills/ai-fill", user, timeout=60.0,
            json={"submitMode": "git", "gitUrl": body.gitUrl.strip()},
        )
        data = resp.json()
    except Exception as e:
        log.warning("调用 SkillLab ai-fill 失败: %s", e)
        raise HTTPException(502, f"无法连接 SkillLab 服务: {e}")
    return JSONResponse(data, status_code=resp.status_code)


@router.post("/skills/submit")
def skill_submit(body: SkillSubmitBody, user: dict = Depends(_require_user)) -> JSONResponse:
    """代理 SkillLab 提交：写入审核分支，状态 reviewing。"""
    if not settings.skilllab_base_url:
        raise HTTPException(400, "SkillLab 同步未配置（SKILLLAB_BASE_URL 为空）")
    headers = {
        "Idempotency-Key": body.idempotencyKey or f"skill-submit:{uuid.uuid4()}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": body.name,
        "description": body.description,
        "domain": body.domain,
        "skillType": body.skillType,
        "tags": body.tags,
        "submitMode": "git",
        "gitUrl": body.gitUrl.strip(),
        "blogUrl": body.blogUrl,
        "authorName": user["username"],
        "isOriginal": body.isOriginal,
        "isUpdate": False,
    }
    try:
        resp = _skilllab_request(
            "POST", "/skills/submit", user, timeout=30.0,
            headers=headers, json=payload,
        )
        data = resp.json()
    except Exception as e:
        log.warning("调用 SkillLab submit 失败: %s", e)
        raise HTTPException(502, f"无法连接 SkillLab 服务: {e}")
    # 提交成功即触发库数据刷新，首页立即可见
    from app.library import store
    store.trigger_refresh()
    return JSONResponse(data, status_code=resp.status_code)


@router.get("/skills/registry")
def skill_registry(user: dict = Depends(_require_user)) -> JSONResponse:
    """代理 SkillLab registry，拿受控标签词表。"""
    if not settings.skilllab_base_url:
        raise HTTPException(400, "SkillLab 同步未配置（SKILLLAB_BASE_URL 为空）")
    try:
        resp = _skilllab_request("GET", "/registry", user, timeout=15.0)
        data = resp.json()
    except Exception as e:
        log.warning("调用 SkillLab registry 失败: %s", e)
        raise HTTPException(502, f"无法连接 SkillLab 服务: {e}")
    return JSONResponse(data, status_code=resp.status_code)


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
.pa-sync{position:fixed;right:24px;bottom:84px;z-index:50;background:#58a6ff;color:#fff;
border:none;padding:11px 20px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;
box-shadow:0 6px 18px rgba(0,0,0,0.45);font-family:inherit;}
.pa-sync:hover{background:#3b9eff;}
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
<button class="pa-sync" onclick="paSync()">同步更新</button>
<button class="pa-fab" onclick="paDelete()">删除项目</button>
<script>
var PA_PID="__PID__";
var PA_STYPE="__SOURCE_TYPE__";
function paDelete(){
  if(!confirm('确认删除该项目？此操作不可恢复。'))return;
  fetch('/projects/'+PA_PID,{method:'DELETE'}).then(function(r){
    if(r.status===403){alert('无权删除该项目');return;}
    if(r.status===401){alert('请先登录');return;}
    if(!r.ok){alert('删除失败');return;}
    alert('已删除');location.href='/';
  }).catch(function(){alert('网络错误');});
}
function paSync(){
  if(PA_STYPE==='zip'){
    var inp=document.createElement('input');inp.type='file';inp.accept='.zip';
    inp.onchange=function(){
      if(!inp.files||!inp.files[0])return;
      if(!confirm('上传新 zip 并重新生成展示页？已生成的展示内容会被覆盖。'))return;
      var fd=new FormData();fd.append('file',inp.files[0]);
      fetch('/projects/'+PA_PID+'/sync',{method:'POST',body:fd}).then(function(r){
        if(r.status===400){alert('请上传 .zip 文件');return;}
        if(r.status===403){alert('无权同步该项目');return;}
        if(r.status===401){alert('请先登录');return;}
        if(r.status===409){alert('项目正在生成中，请稍后再试');return;}
        if(!r.ok){alert('同步失败');return;}
        location.href='/projects/'+PA_PID+'/progress';
      }).catch(function(){alert('网络错误');});
    };
    inp.click();
    return;
  }
  if(!confirm('从源仓库重新拉取代码并更新展示页？已生成的展示内容会被覆盖。'))return;
  fetch('/projects/'+PA_PID+'/sync',{method:'POST'}).then(function(r){
    if(r.status===403){alert('无权同步该项目');return;}
    if(r.status===401){alert('请先登录');return;}
    if(r.status===409){alert('项目正在生成中，请稍后再试');return;}
    if(!r.ok){alert('同步失败');return;}
    location.href='/projects/'+PA_PID+'/progress';
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


def _inject_auth_tools(html: str, project_id: str, source_type: str = "") -> str:
    snippet = (
        _AUTH_SNIPPET
        .replace("__PID__", project_id)
        .replace("__SOURCE_TYPE__", source_type or "")
    )
    if "</body>" in html:
        return html.replace("</body>", snippet + "</body>", 1)
    return html + snippet


_FAV_SNIPPET = """
<style>
.pa-fav{display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border-radius:8px;
  border:1px solid #30363d;background:#1f2428;color:#c9d1d9;font-size:13px;cursor:pointer;
  font-family:inherit;transition:all 0.15s;}
.pa-fav:hover{border-color:#58a6ff;color:#58a6ff;}
.pa-fav.on{color:#f5c518;border-color:rgba(245,197,24,0.4);background:rgba(245,197,24,0.08);}
</style>
<script>
(function(){
  var pid="__PID__";
  var links=document.querySelector('.pa-src-links');
  if(!links)return;
  var btn=document.createElement('button');
  btn.className='pa-fav';
  btn.textContent='☆ 收藏';
  btn.onclick=function(){
    var on=btn.classList.contains('on');
    fetch('/projects/'+pid+'/favorite',{method:on?'DELETE':'POST'}).then(function(r){
      if(r.status===401){alert('请先登录');location.href='/sso';return;}
      if(!r.ok){alert('操作失败');return;}
      btn.classList.toggle('on',!on);
      btn.textContent=(!on?'★':'☆')+' 收藏';
    }).catch(function(){alert('网络错误');});
  };
  links.appendChild(btn);
  fetch('/projects/'+pid+'/favorite/status').then(function(r){return r.json();}).then(function(d){
    if(d&&d.favorited){btn.classList.add('on');btn.textContent='★ 收藏';}
  }).catch(function(){});
})();
</script>
"""


def _inject_favorite_button(html: str, project_id: str) -> str:
    snippet = _FAV_SNIPPET.replace("__PID__", project_id)
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
    # 模板版本落后或缺编辑标记时，按存储的 JSON 重新渲染升级（不调 LLM）
    from app.llm.renderer import render_page, TEMPLATE_VERSION
    if ((proj.get("template_version", 1) < TEMPLATE_VERSION
         or "data-field" not in html or "pa-src-links" not in html)
            and proj.get("generated_json") and proj.get("parsed_json")):
        try:
            import json as _json
            html = render_page(
                _json.loads(proj["parsed_json"]), _json.loads(proj["generated_json"]),
                project_id=project_id,
                source=proj.get("source") or "",
                source_type=proj.get("source_type") or "",
            )
            html_path.write_text(html, encoding="utf-8")
            set_template_version(project_id, TEMPLATE_VERSION)
        except Exception:
            log.warning("老页面升级失败: %s", project_id)
    html = _inject_favorite_button(html, project_id)
    if _can_modify(_current_user(request), proj):
        html = _inject_auth_tools(html, project_id, proj.get("source_type") or "")
    return HTMLResponse(html)


def _zip_dir(src_dir: Path, dest_zip: Path) -> None:
    """把目录打包成 zip，排除 .git。dest_zip 须是不存在的文件路径。"""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src_dir.rglob("*"):
            if ".git" in path.parts:
                continue
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))


@router.get("/projects/{project_id}/download")
def download_project(project_id: str):
    """下载项目源码包：zip 上传的直接返回原包；url/local 现场打包 repos/{id}（排除 .git）。"""
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if proj["status"] != TaskStatus.DONE.value:
        raise HTTPException(409, f"项目尚未就绪，当前状态: {proj['status']}")

    source_type = proj.get("source_type")
    base_name = proj.get("source") or project_id

    if source_type == "zip":
        zip_path = settings.uploads_dir / f"{project_id}.zip"
        if not zip_path.exists():
            raise HTTPException(404, "原始压缩包已丢失")
        return FileResponse(
            str(zip_path),
            media_type="application/zip",
            filename=f"{project_id}.zip",
        )

    repo_dir = settings.repos_dir / project_id
    if not repo_dir.exists():
        raise HTTPException(404, "项目源码目录已丢失")

    tmp = Path(tempfile.mkstemp(suffix=".zip", prefix=f"pa_{project_id}_")[1])
    _zip_dir(repo_dir, tmp)
    return FileResponse(
        str(tmp),
        media_type="application/zip",
        filename=f"{project_id}.zip",
        background=BackgroundTask(lambda: _cleanup_tmp(tmp)),
    )


def _cleanup_tmp(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        log.warning("清理临时 zip 失败: %s", path)


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


@router.post("/projects/{project_id}/sync")
async def sync_project_api(
    project_id: str,
    request: Request,
    file: Optional[UploadFile] = File(None),
) -> JSONResponse:
    """同步更新：重新拉取源码并重生展示页。仅作者或管理员。

    zip 项目需上传新 zip 覆盖旧包；url/local 直接重新拉取。
    源码无变化时（source_hash 命中）由任务层跳过 LLM。生成中拒绝重复触发。
    """
    user = _require_user(request)
    proj = get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not _can_modify(user, proj):
        raise HTTPException(403, "无权同步该项目")
    if proj["status"] in (
        TaskStatus.CLONING.value, TaskStatus.PARSING.value, TaskStatus.GENERATING.value,
    ):
        raise HTTPException(409, "项目正在生成中，请稍后再试")

    if proj.get("source_type") == "zip":
        if not file or not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(400, "zip 项目需上传新的 .zip 压缩包")
        settings.ensure_dirs()
        zip_path = settings.uploads_dir / f"{project_id}.zip"
        with zip_path.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
        log.info("项目 %s 更新 zip 包: %s", project_id, file.filename)

    update_status(project_id, TaskStatus.PENDING, 0, "准备同步更新...")
    _dispatch(project_id, task=sync_project)
    log.info("项目 %s 被 %s 触发同步更新", project_id, user.get("username"))
    return JSONResponse({"project_id": project_id, "status": "pending"})


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
    html = render_page(
        parsed, gen,
        project_id=project_id,
        source=proj.get("source") or "",
        source_type=proj.get("source_type") or "",
    )
    Path(proj["html_path"]).write_text(html, encoding="utf-8")
    log.info("项目 %s 的 %s 被 %s 编辑", project_id, body.field, user.get("username"))
    return JSONResponse({"ok": True})


def _normalize_git_url(url: str) -> str:
    """规范化 git URL 用于匹配：去协议/认证信息/.git 后缀，小写。"""
    url = (url or "").strip().lower()
    if "://" in url:
        url = url.split("://", 1)[1]
    if "@" in url:  # 去掉 user:token@ 认证前缀
        url = url.split("@")[-1]
    url = url.removesuffix(".git").rstrip("/")
    return url


@router.post("/integrations/gitlab-webhook")
async def gitlab_webhook(request: Request) -> JSONResponse:
    """GitLab Push Webhook 回调：验证密钥后匹配仓库，触发库数据实时刷新。"""
    from app.library import store

    if not settings.library_webhook_secret:
        raise HTTPException(400, "Webhook 未配置（LIBRARY_WEBHOOK_SECRET 为空）")
    token = request.headers.get("X-Gitlab-Token", "")
    if token != settings.library_webhook_secret:
        raise HTTPException(403, "无效的 webhook token")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": True, "matched": False})

    if body.get("object_kind") != "push":
        return JSONResponse({"ok": True, "matched": False})

    project = body.get("project") or {}
    repo_urls = [
        project.get("git_http_url"), project.get("http_url"),
        project.get("url"), (body.get("repository") or {}).get("url"),
        project.get("path_with_namespace"), project.get("path"),
    ]
    repo_urls = {_normalize_git_url(u) for u in repo_urls if u}

    targets = {
        _normalize_git_url(settings.library_tool_repo_url): "tool",
        _normalize_git_url(settings.library_skill_repo_url): "skill",
    }
    matched = None
    for u in repo_urls:
        for target_url, name in targets.items():
            if target_url and (u == target_url or u.endswith(target_url) or target_url.endswith(u)):
                matched = name
                break
        if matched:
            break

    ref = body.get("ref") or ""
    branch = ref.removeprefix("refs/heads/")
    if matched and not store.is_configured():
        matched = None

    if matched:
        store.trigger_refresh()
        log.info("GitLab webhook 触发库刷新: %s (branch=%s)", matched, branch)
    return JSONResponse({"ok": True, "matched": matched, "branch": branch})


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
    request: Request,
    page: int = 1,
    per_page: int = 24,
    lang: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
) -> JSONResponse:
    """项目列表 JSON API：分页 + 语言/标签筛选 + 模糊搜索。登录用户的收藏项目排前面。"""
    from app.models.models import list_cards
    user = _current_user(request)
    cards, total = list_cards(
        page=page, per_page=per_page, lang=lang, tag=tag,
        user_id=user["tforum_user_id"] if user else None, q=q,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse({
        "cards": cards,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    })


@router.post("/projects/{project_id}/favorite")
def favorite_project(project_id: str, user: dict = Depends(_require_user)) -> JSONResponse:
    if not get_project(project_id):
        raise HTTPException(404, "项目不存在")
    add_favorite(user["tforum_user_id"], project_id)
    return JSONResponse({"favorited": True})


@router.delete("/projects/{project_id}/favorite")
def unfavorite_project(project_id: str, user: dict = Depends(_require_user)) -> JSONResponse:
    remove_favorite(user["tforum_user_id"], project_id)
    return JSONResponse({"favorited": False})


@router.get("/projects/{project_id}/favorite/status")
def favorite_status(project_id: str, request: Request) -> JSONResponse:
    user = _current_user(request)
    return JSONResponse({
        "favorited": get_favorite_status(
            user["tforum_user_id"] if user else None, project_id
        ),
        "logged_in": user is not None,
    })


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    page: int = 1,
    lang: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
) -> HTMLResponse:
    """社区首页 = 最新发布列表页（项目 + 工具/技能库条目混排）。"""
    from app.models.models import list_cards, distinct_filter_values, project_url_map
    from app.library import store as library_store
    from app.llm.renderer import render_template

    per_page = 24
    user = _current_user(request)
    cards, proj_total = list_cards(
        page=page, per_page=per_page, lang=lang, tag=tag,
        user_id=user["tforum_user_id"] if user else None, q=q,
    )

    # 工具/技能库条目并入信息流；语言/标签筛选只针对项目，激活时不混入
    lib_entries: list[dict] = []
    lib_error = ""
    if library_store.is_configured() and not (lang or tag):
        if not library_store.stats().get("loaded_at"):
            library_store.refresh()
        lib_error = library_store.stats().get("error") or ""
        lib_entries = library_store.get_entries(source="", q=q or "")

    # 统一分页：项目在前（按发布时间），库条目随后
    start_idx = (page - 1) * per_page
    end_idx = page * per_page
    lib_from = max(0, start_idx - proj_total)
    lib_to = max(0, end_idx - proj_total)
    page_entries = lib_entries[lib_from:lib_to]

    url_map = project_url_map()
    for e in page_entries:
        norm = (e.get("repo_url") or "").strip().lower().removesuffix(".git").rstrip("/")
        e["project"] = url_map.get(norm)

    total = proj_total + len(lib_entries)
    total_pages = max(1, (total + per_page - 1) // per_page)

    # 分页页码窗口（最多显示 7 个）
    start = max(1, page - 3)
    end = min(total_pages, start + 6)
    start = max(1, end - 6)
    page_range = list(range(start, end + 1))

    html = render_template(
        "list.html",
        cards=cards,
        lib_entries=page_entries,
        lib_error=lib_error,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        page_range=page_range,
        lang=lang,
        tag=tag,
        q=q,
        langs=distinct_filter_values("lang"),
        tags=distinct_filter_values("tag"),
        current_user=user,
        toolsync_enabled=bool(settings.toolsync_base_url),
        skilllab_enabled=bool(settings.skilllab_base_url),
    )
    return HTMLResponse(html)


@router.get("/sso")
def sso_entry(request: Request, token: Optional[str] = None):
    """tForum 外链跳转入口：服务端校验 token → 建会话 → 回首页。

    tForum 管理后台把外部栏目 URL 配成 {PROJECTAGENT_PUBLIC_URL}/sso?token={token}，
    用户点击后 tForum 前端用 window.open 打开最终 URL，本路由拿到 token 去问 tForum 校验。

    本站会话仍有效时，token 缺失/过期/校验服务不可达均无感放行进首页，
    不再强制用户重新走 tForum 登录。
    """
    existing = _current_user(request)
    if existing:
        home_redirect = RedirectResponse(url="/", status_code=303)
        # token 有效时仍走完整流程（顺便刷新 tforum_token）；无效则直接放行
        if not token:
            return home_redirect
    if not token:
        return _sso_fail_page("缺少登录凭证，请从 tForum 站内入口进入。")

    verify_url = f"{settings.tforum_base_url.rstrip('/')}/api/v1/user/verifyToken"
    try:
        resp = httpx.get(verify_url, params={"token": token}, timeout=10.0)
        data = resp.json()
    except Exception as e:
        log.warning("调用 tForum verifyToken 失败: %s", e)
        if existing:
            return home_redirect
        return _sso_fail_page("无法连接登录服务，请稍后重试。")

    if data.get("code") != 0 or not data.get("data"):
        if existing:
            return home_redirect
        msg = data.get("message") or "token 无效"
        return _sso_fail_page(f"登录校验失败：{msg}")

    info = data["data"]
    user = upsert_user(info)
    set_tforum_token(user["tforum_user_id"], token)
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
