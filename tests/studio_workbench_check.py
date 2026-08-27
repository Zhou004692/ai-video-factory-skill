from pathlib import Path
import os

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "studio_workbench_validation"
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get(
            "PLAYWRIGHT_BROWSER_PATH",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ),
    )
    page = browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=1)
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")

    page.wait_for_selector("#studio-workbench")
    assert page.locator("#global-project-list [data-global-project]").count() > 0
    assert page.locator("#studio-project-title").inner_text() != "选择一个项目开始"
    assert page.locator("#studio-inspector-next").count() == 0
    assert page.locator("#studio-primary-action").is_visible()
    assert page.locator("#studio-workbench .primary-action").count() == 1
    initial_workspace = page.evaluate(
        """() => {
          const project = state.projects.projects.find(
            item => !['production_setup', 'production'].includes(item.stage)
          );
          return openProjectStudio(project, project.stage).then(() => ({stage: project.stage}));
        }"""
    )
    page.wait_for_selector(f'[data-studio-stage="{initial_workspace["stage"]}"].active')
    assert page.locator("#studio-stage-status").inner_text() in {
        "已完成", "当前阶段", "可查看", "未开始"
    }
    assert page.locator(".studio-outline .studio-pane-role").inner_text() == "01 · 选择对象"
    assert page.locator(".studio-document-head .studio-pane-role").inner_text() == "02 · 内容工作区"
    assert page.locator(".studio-work-state").is_visible()
    assert "当前对象" in page.locator(".studio-work-state").inner_text()
    assert "业务状态" in page.locator(".studio-work-state").inner_text()
    assert "编辑权限" in page.locator(".studio-work-state").inner_text()
    assert page.locator(".studio-inspector>header .section-no").inner_text() == "03 / STAGE DECISION"
    assert page.get_by_text("进入下一阶段前需满足", exact=True).is_visible()
    assert page.get_by_text("本阶段产物", exact=True).is_visible()
    assert page.get_by_text("影响下一阶段", exact=True).is_visible()
    assert page.get_by_text("项目上下文", exact=True).count() == 0
    assert page.locator('.nav-item[data-page="overview"]').is_visible()
    assert page.locator('.nav-item[data-page="todos"]').is_visible()
    assert page.locator('.nav-item[data-page="characters"]').is_visible()
    assert page.locator('.nav-item[data-page="config"]').is_visible()
    assert not page.locator('.nav-item[data-page="scripts"]').is_visible()
    assert page.locator("#open-advanced-nav").is_visible()
    assert page.locator('[data-global-filter="video-review"]').is_visible()
    review_collection = page.locator('[data-global-filter="review"]')
    review_count = int(page.locator("#global-review-count").inner_text())
    review_collection.click()
    assert "active" in (review_collection.get_attribute("class") or "")
    assert page.locator("#global-project-list-title").inner_text() == "待内容决策"
    assert page.locator("#global-project-list [data-global-project]").count() == review_count
    page.screenshot(path=str(OUT / "studio-smart-collection.png"), full_page=False)
    page.locator("#global-filter-reset").click()
    assert page.locator("#global-project-list-title").inner_text() == "全部项目"
    assert "active" in (page.locator("#global-filter-reset").get_attribute("class") or "")
    all_count = int(page.locator("#global-project-list-count").inner_text().split()[0])
    assert page.locator("#global-project-list [data-global-project]").count() == all_count
    layout = page.evaluate(
        """() => {
          const box = selector => document.querySelector(selector).getBoundingClientRect();
          const workbench = box('#studio-workbench');
          const rail = box('.sidebar');
          const library = box('.global-project-panel');
          const head = box('.studio-project-head');
          const stages = box('#studio-stages');
          const stageAction = box('#studio-primary-action');
          const canvas = box('.studio-canvas');
          const inspector = box('.studio-inspector');
          return {workbench, rail, library, head, stages, stageAction, canvas, inspector};
        }"""
    )
    assert abs(layout["head"]["left"] - layout["workbench"]["left"]) < 1
    assert abs(layout["rail"]["width"] - 64) < 1
    assert abs(layout["library"]["width"] - 270) < 1
    assert abs(layout["head"]["height"] - 70) < 1
    assert abs(layout["stages"]["height"] - 56) < 1
    assert layout["stageAction"]["top"] >= layout["stages"]["top"]
    assert layout["stageAction"]["bottom"] <= layout["stages"]["bottom"]
    assert layout["stageAction"]["right"] <= layout["stages"]["right"] - 12
    assert abs(layout["canvas"]["left"] - layout["workbench"]["left"] - 16) < 1
    assert abs(layout["inspector"]["width"] - 270) < 1
    assert abs(layout["workbench"]["right"] - layout["inspector"]["right"] - 16) < 1
    assert abs(layout["head"]["right"] - layout["workbench"]["right"]) < 1
    assert abs(layout["stages"]["right"] - layout["workbench"]["right"]) < 1
    assert abs(layout["canvas"]["top"] - layout["inspector"]["top"]) < 1
    page.screenshot(path=str(OUT / "studio-source.png"), full_page=False)

    split_timeline = page.evaluate(
        """() => {
          for (const project of state.projects.projects) {
            const video = studioRelated(project).video;
            const split = (video?.director_timeline || []).find(
              item => Number(item.video_master_idx) !== Number(item.audio_master_idx)
            );
            if (split) return {
              projectId: project.project_id,
              masterIndex: Number(split.video_master_idx),
              relatedCount: studioMasterScenes(
                video,
                studioVideoMasters(video).find(
                  master => Number(master.idx) === Number(split.video_master_idx)
                )
              ).length,
              totalCount: video.director_timeline.length,
            };
          }
          return null;
        }"""
    )
    assert split_timeline
    page.evaluate(
        """target => openProjectStudio(
          state.projects.projects.find(project => project.project_id === target.projectId),
          'video',
          {masterIndex: target.masterIndex, videoView: 'timeline'}
        )""",
        split_timeline,
    )
    page.wait_for_selector('[data-studio-stage="video"].active')
    assert page.locator(".studio-timeline-beat").count() == split_timeline["relatedCount"]
    assert page.locator(".studio-timeline-beat.muted").count() == 0
    assert "当前母片" in page.locator(".studio-timeline-summary").inner_text()
    assert page.locator("#studio-timeline-scope").inner_text() == "查看全片时间线"
    page.locator("#studio-timeline-scope").click()
    assert page.locator(".studio-timeline-beat").count() == split_timeline["totalCount"]
    assert page.locator("#studio-timeline-scope").inner_text() == "仅看当前母片"
    page.locator("#studio-timeline-scope").click()
    page.screenshot(path=str(OUT / "studio-video-current-master-timeline.png"), full_page=False)

    page.locator('[data-studio-video-view="anchors"]').click()
    person_anchor = page.locator('[data-studio-anchor-preview="character"]').first
    scene_anchor = page.locator('[data-studio-anchor-preview="scene"]').first
    assert person_anchor.count() == 1
    assert scene_anchor.count() == 1
    person_box = person_anchor.bounding_box()
    scene_box = scene_anchor.bounding_box()
    assert person_box["width"] >= 108
    assert person_box["height"] >= 132
    assert scene_box["width"] >= 172
    assert scene_box["height"] >= 96
    person_anchor.click()
    assert page.locator("#modal").is_visible()
    assert page.locator(".studio-anchor-lightbox img").is_visible()
    assert page.locator(".studio-anchor-lightbox img").evaluate(
        "image => image.complete && image.naturalWidth > 0"
    )
    page.locator("#modal-close").click()
    scene_anchor.click()
    assert page.locator("#modal").is_visible()
    assert "场景锚点" in page.locator("#modal-title").inner_text()
    page.screenshot(path=str(OUT / "studio-anchor-preview.png"), full_page=False)
    page.locator("#modal-close").click()

    for stage in ("brief", "script", "video", "review", "publish"):
        button = page.locator(f'[data-studio-stage="{stage}"]')
        if not button.is_disabled():
            button.click()
            page.wait_for_timeout(120)
            assert "active" in (button.get_attribute("class") or "")
            assert page.locator(".studio-stage-workspace, .studio-video-workspace").count() == 1
            left_role = page.locator(
                ".studio-outline .studio-pane-role, .studio-master-plan .studio-pane-role"
            )
            center_role = page.locator(
                ".studio-document-head .studio-pane-role, .studio-video-editor .studio-pane-role"
            )
            assert left_role.inner_text() == "01 · 选择对象"
            assert center_role.inner_text() == "02 · 内容工作区"
            assert page.get_by_text("本阶段产物", exact=True).is_visible()
            assert page.locator(".studio-work-state").is_visible()
            if stage == "brief":
                brief_state = page.locator(".studio-work-state").inner_text()
                assert "角度" in brief_state
                assert any(label in brief_state for label in ("已选定", "系统推荐", "备选方向"))
            if stage == "video":
                assert page.get_by_text("母片计划", exact=True).is_visible()
                assert page.get_by_text("导演时间线", exact=True).is_visible()
            page.screenshot(path=str(OUT / f"studio-{stage}.png"), full_page=False)

    assert page.locator("#studio-task-drawer").is_visible()
    assert page.locator("#studio-task-drawer").bounding_box()["width"] <= 50
    page.locator("#studio-task-toggle").click()
    assert page.locator("#studio-task-drawer").get_attribute("class").endswith("open")
    page.wait_for_timeout(260)
    assert page.locator("#studio-task-drawer").bounding_box()["width"] >= 300

    current_project_id = page.evaluate("() => state.studioProjectId")
    second = page.locator(
        f'#global-project-list [data-global-project]:not([data-global-project="{current_project_id}"])'
    ).first
    if second.count():
        before = page.locator("#studio-project-title").inner_text()
        expected_project_id = second.get_attribute("data-global-project")
        second.click()
        page.wait_for_timeout(120)
        assert page.locator("#studio-project-title").inner_text() != before
        selected_projects = page.locator(
            "#global-project-list [data-global-project].selected"
        )
        assert selected_projects.count() == 1
        assert selected_projects.get_attribute("data-global-project") == expected_project_id

    review_project_id = page.evaluate(
        """() => state.projects.projects.find(
          project => project.next_action === 'review_script'
        )?.project_id || ''"""
    )
    assert review_project_id
    page.evaluate(
        """projectId => openProjectStudio(
          state.projects.projects.find(project => project.project_id === projectId),
          'brief'
        )""",
        review_project_id,
    )
    page.wait_for_selector('[data-studio-stage="brief"].active')
    assert page.locator("#studio-primary-action").is_hidden()
    assert "剧本决策" in page.locator("#studio-return-current").inner_text()
    page.locator("#studio-return-current").click()
    page.wait_for_selector('[data-studio-stage="script"].active')
    assert page.locator("#studio-primary-action").is_visible()
    assert page.locator("#studio-primary-action").inner_text().startswith("审核当前版本")
    page.locator("#studio-primary-action").click()
    page.wait_for_selector('#drawer[aria-hidden="false"]')
    expected_title = page.locator(
        ".studio-outline [data-studio-outline].active b"
    ).inner_text()
    assert page.locator("#drawer-title").inner_text() == expected_title
    page.locator("#drawer-close").click()
    versions = page.locator(".studio-outline [data-studio-outline]")
    assert versions.count() >= 2
    target = versions.nth(1)
    expected_title = target.locator("b").inner_text()
    target.click()
    assert "V2" in page.locator(".studio-work-state").inner_text()
    assert "V2" in page.locator("#studio-primary-action").inner_text()
    page.locator("#studio-primary-action").click()
    page.wait_for_selector('#drawer[aria-hidden="false"]')
    assert page.locator("#drawer-title").inner_text() == expected_title
    page.screenshot(
        path=str(OUT / "studio-script-version-selection.png"), full_page=False
    )
    page.locator("#drawer-close").click()

    brief_project_id = page.evaluate(
        """() => state.projects.projects.find(project => {
          const related = studioRelated(project);
          return project.next_action === 'generate_scripts'
            && related.brief?.status === 'candidate'
            && (related.brief?.brief?.angles || []).length >= 2;
        })?.project_id || ''"""
    )
    assert brief_project_id
    page.evaluate(
        """projectId => openProjectStudio(
          state.projects.projects.find(project => project.project_id === projectId),
          'brief'
        )""",
        brief_project_id,
    )
    page.locator('.studio-outline [data-studio-outline]').nth(1).click()
    assert page.locator("#studio-primary-action").inner_text() == "使用当前角度生成剧本"
    page.locator("#studio-primary-action").click()
    page.wait_for_selector('#drawer[aria-hidden="false"]')
    assert page.locator('input[name="brief-angle"]:checked').get_attribute("value") == "1"
    page.screenshot(path=str(OUT / "studio-brief-angle-selection.png"), full_page=False)
    page.locator("#drawer-close").click()

    video_project_id = page.evaluate(
        """() => state.projects.projects.find(project => {
          const video = studioRelated(project).video;
          return video && (video.shots || []).length >= 2;
        })?.project_id || ''"""
    )
    assert video_project_id
    page.evaluate(
        """projectId => openProjectStudio(
          state.projects.projects.find(project => project.project_id === projectId),
          'video'
        )""",
        video_project_id,
    )
    selected_master_index = str(
        page.evaluate(
            """projectId => studioRelated(
              state.projects.projects.find(project => project.project_id === projectId)
            ).video.shots[1].idx""",
            video_project_id,
        )
    )
    selected_master = page.locator(
        f'[data-studio-master="{selected_master_index}"]'
    )
    assert selected_master.count() == 1
    selected_master.click()
    page.locator("#studio-open-video").click()
    page.wait_for_selector('#drawer[aria-hidden="false"]')
    assert page.locator(".master-row.focused").get_attribute("data-video-master") == selected_master_index
    page.screenshot(path=str(OUT / "studio-master-selection.png"), full_page=False)
    page.locator("#drawer-close").click()

    publication_id = page.evaluate(
        """() => (state.publishing.publications || [])[0]?.publication_id || ''"""
    )
    if publication_id:
        expected_platform = page.evaluate(
            """publicationId => state.publishing.publications.find(
              item => item.publication_id === publicationId
            ).platform""",
            publication_id,
        )
        page.evaluate(
            """publicationId => openEntityInProject(
              publicationId, 'performance', 'publish'
            )""",
            publication_id,
        )
        page.wait_for_selector('[data-studio-stage="publish"].active')
        assert "active" in (
            page.locator(f'[data-studio-publish-channel="{expected_platform}"]').get_attribute("class")
            or ""
        )
        assert "当前渠道" in page.locator("#studio-primary-action").inner_text()
        page.locator("#studio-primary-action").click()
        page.wait_for_selector('#drawer[aria-hidden="false"]')
        assert page.locator("#drawer-body .drawer-summary").inner_text().startswith(
            "抖音" if expected_platform == "douyin" else "微信视频号"
        )
        page.screenshot(path=str(OUT / "studio-publish-channel-selection.png"), full_page=False)
        page.locator("#drawer-close").click()

    ready_job_id = page.evaluate(
        """() => (state.publishing.ready || []).find(item =>
          (item.remaining_platforms || []).includes('weixin_channels')
        )?.job_id || ''"""
    )
    if ready_job_id:
        ready_project_id = page.evaluate(
            """jobId => state.projects.projects.find(
              project => project.video?.job_id === jobId
            )?.project_id || ''""",
            ready_job_id,
        )
        assert ready_project_id
        page.evaluate(
            """projectId => openProjectStudio(
              state.projects.projects.find(project => project.project_id === projectId),
              'publish', {outlineIndex: 1}
            )""",
            ready_project_id,
        )
        assert "当前渠道" in page.locator("#studio-primary-action").inner_text()
        page.locator("#studio-primary-action").click()
        page.wait_for_selector('#drawer[aria-hidden="false"]')
        assert "selected" in (
            page.locator('[data-publish-method="weixin_channels"]').get_attribute("class")
            or ""
        )
        assert page.locator(
            '[data-publish-method="weixin_channels"]'
        ).get_attribute("aria-current") == "true"
        page.screenshot(path=str(OUT / "studio-ready-channel-selection.png"), full_page=False)
        page.locator("#drawer-close").click()

    assert not errors, errors
    browser.close()

print("STUDIO_WORKBENCH_OK")
