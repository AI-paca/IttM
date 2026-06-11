## Roadmap / История разработки курсового проекта

Ниже сохранены исторические диаграммы процесса разработки курса и планов (Roadmaps), которые иллюстрируют этапы ветвления (HW2, HW3, и т.д.):

<details>
<summary>Mermaid: HW and core branches</summary>

```mermaid
gantt
    title IttM HW and core branches
    dateFormat  YYYY-MM-DD
    axisFormat  %m-%d

    section UI
    feature/hw2-mvp -> hw3             :ui_hw, 2026-05-01, 2026-05-22
    post-hw5 UI update                 :ui_update, 2026-06-05, 2026-06-12

    section Input / source
    feature/hw2-mvp -> hw3 sources     :input_hw, 2026-05-01, 2026-05-22

    section Gateway API
    feature/hw2-mvp -> hw3             :api_hw, 2026-05-01, 2026-05-22

    section OCR backend
    feature/hw2-mvp -> mid-hw4 -> engine :ocr_hw, 2026-05-01, 2026-06-08

    section Launch modes
    feature/hw2-mvp -> hw4 stable modes :run_modes, 2026-05-01, 2026-05-29

    section CI/CD
    hw2 Pages workflow                 :ci_pages, 2026-05-08, 2026-05-15
    hw3 -> hw5 lint/build gate         :ci_hw, 2026-05-18, 2026-06-12

    section Testing
    mid hw3 -> hw5 test suites         :tests_hw, 2026-05-18, 2026-06-12

    section Security
    hw6 -> hw7 SAST/SCA/SBOM           :security_hw, 2026-06-12, 2026-06-26

    section Docs
    late hw3 -> hw8 docs               :docs_hw, 2026-05-19, 2026-07-03
```

</details>

<details>
<summary>Mermaid: development branches</summary>

```mermaid
%%{init: {"themeVariables": {"activeTaskBkgColor": "#7db8e8", "activeTaskBorderColor": "#dbeeff", "activeTaskTextColor": "#20242a"}}}%%
gantt
    title IttM development branches
    dateFormat  YYYY-MM-DD
    axisFormat  %m-%d

    section UI
    browser extension                  :active, dev_ext, 2026-07-08, 4d

    section Input import
    HTML/canvas                        :active, dev_html, 2026-06-24, 3d
    AI Studio files                    :active, dev_ai, 2026-06-27, 3d

    section Launch modes
    Hypr utility C/Rust/Go + UI         :active, dev_hypr_utility, 2026-07-12, 8d

    section Docs
    development docs                   :active, dev_docs, 2026-06-30, 2026-07-23
```

</details>
