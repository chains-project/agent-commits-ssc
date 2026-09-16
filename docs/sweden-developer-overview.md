# Developers with Public Locations in Sweden: Top-10 Research and Development Profiles

## Background and method

This report uses an existing corpus of agent-associated GitHub commits to describe developers whose public locations match Sweden. It presents their technical interests, representative projects and commit distributions, with aggregate statistics, ten individual profiles and a Top-100 Excel workbook. The analysis covers identifiable public accounts and their associated commits, rather than a census of developers in Sweden.

**Corpus and the meaning of expanded.** The observation period runs from 2025-06-01 to 2026-05-31 and covers Claude, Codex, GitHub Copilot and Cursor. The baseline *matched* corpus uses a common daily ten-minute sampling window for all four tools and contains 460,645 agent–repository–SHA records. The *expanded* corpus combines this baseline with daily four-hour sampling and supplementary audit records for Codex, Copilot and Cursor. After merging and deduplication, it contains 1,853,783 agent–repository–SHA records, representing 1,852,606 distinct repository–SHA pairs after deduplication across agents. A record indicates an association between an agent and a commit in a repository; it is not a distinct developer or a tool invocation.

The expanded corpus increases the coverage of observable commits and accounts, reducing omissions associated with the shorter sampling window. It does not extend Claude's sampling coverage and is not continuous collection throughout the year. Observation intensity therefore differs across tools. This report uses expanded as its primary corpus and treats differences between tools as descriptive results, not as usage comparisons under equivalent sampling conditions.

**Account identification and location filtering.** Commit authors and co-authors are resolved to GitHub numeric account IDs where possible. Records for the same account are combined, and identified bots, organizations and agent identities are excluded. Public profiles are then queried for the existing candidate set. Accounts qualify when their location explicitly states Sweden/Sverige or matches an accepted Swedish city or region. Filtering covers the full existing candidate set rather than searching only within a global Top-N. Blank or ambiguous locations are not automatically included.

**Counting, ranking and developer profiles.** Each account's associated commit SHAs are combined into a set. A SHA appearing under multiple repositories, tools or roles counts once in that account's total. Accounts are ordered by unique SHA count descending. The Top-100 table contains exactly 100 accounts, with ties ordered by numeric account ID ascending. Associated accounts, unique SHAs and repositories are also counted separately for each tool; overall totals are further deduplicated across accounts. Public profiles and project documentation are used to summarize the ten developers' technical directions and verify voluntarily published contact email addresses. This contextual material does not change ranking scores.

## Overall results

The dataset contains **1,052 accounts** whose public locations satisfy the Sweden criteria, associated with **10,060 unique commit SHAs across 1,854 repositories**. The top 100 accounts cover 7,232 unique SHAs, or 71.9% of the total.

|Agent|Associated Accounts|Account Share|Unique SHAs|Repositories|
|---|---:|---:|---:|---:|
|Claude|400|38.0%|1,014|655|
|Codex|13|1.2%|132|96|
|GitHub Copilot|606|57.6%|8,446|1,080|
|Cursor|96|9.1%|503|212|

**Tool distribution.** Copilot is associated with 606 accounts, or 57.6% of eligible accounts, and 8,446 unique SHAs, covering approximately 84.0% of the overall SHA set. Claude is associated with 400 accounts, or 38.0%, and 1,014 unique SHAs. Cursor and Codex are associated with 96 and 13 accounts respectively. Copilot-associated records are the most numerous in this corpus. However, expanded sampling increases coverage for the non-Claude tools, so these proportions should not be interpreted as Swedish market shares or adoption rates.

**Multiple-tool associations.** The sample associates 56 accounts with more than one tool, representing 5.3% of eligible accounts. The remaining 996 accounts have only one observed tool association. Account shares can therefore overlap, and commit counts should not simply be added across tools. Observing one tool does not establish that a developer has never used another tool in a different project or period.

**Commit concentration.** The top 100 accounts represent approximately 9.5% of eligible accounts but cover 71.9% of unique SHAs. The median account has two associated SHAs, and 414 accounts have only one. The distribution is concentrated with a long tail: a small set of accounts covers most observable records, while many accounts appear only sparsely in the sample. The Top-10 and Top-100 describe accounts with many observed associations, not the typical workflow of all developers. The complete Top-100 workbook is linked at the end of this report.

The ten developers' technical directions fall into four overlapping groups: security and public-data tools (pethers); systems software, compilers and development infrastructure (1313, GregorGullwi, rmstdope and Hexagon); games, procedural simulation and systems automation (timmiee and feinorgh); and device integration and web applications (R00S, pownas and hessius). These groups summarize public projects rather than formal research appointments.

## Top-10 overview

Locations reflect publicly stated countries or cities. A dash means no public contact email was verified in this review. Company inboxes are explicitly labelled. Each developer's GitHub profile is linked once in the corresponding individual section.

|Rank|Developer / GitHub|Public Location|Research and Development Focus|Unique SHAs|Repositories|Public Email|
|---|---|---|---|---:|---:|---|
|1|James Pether Sörling / pethers|Gothenburg, Sweden|Cybersecurity, DevSecOps and public-data tools|2568|10|[info@hack23.com](https://hack23.com/) (Company inbox)|
|2|Roos / R00S|Sweden|Smart homes, device integration and kitchen automation|381|6|[roos@roos.tc](https://api.github.com/users/R00S) (GitHub public email)|
|3|Per Johansson / 1313|Stockholm|Rust systems, Apple platform APIs and multimedia|340|77|—|
|4|Gregor Gullwi / GregorGullwi|Sweden|Compilers, code generation and performance|324|1|—|
|5|timmiee|Sweden|Browser-based 3D games and real-time interaction|252|4|[timmie_tooth@live.com](https://api.github.com/users/timmiee) (GitHub public email)|
|6|rmstdope|Linköping, Sweden|Rust emulation and cross-platform systems|204|3|[henrik@kurelid.se](https://api.github.com/users/rmstdope) (GitHub public email)|
|7|Pär Karlsson / feinorgh|Sweden|Procedural world generation and systems automation|186|3|[feinorgh@gmail.com](https://api.github.com/users/feinorgh) (GitHub public email)|
|8|Hexagon|Sweden|JavaScript/TypeScript runtimes, developer tools and Rust|173|13|[hexagon@56k.guru](https://api.github.com/users/Hexagon) (GitHub public email)|
|9|Jonas Arvidson / pownas|Örebro, Sweden|.NET/Blazor, websites and practical applications|147|7|pownas@outlook.com (Public profile email)|
|10|hessius|Sweden|AI integration, device software and user interfaces|135|3|—|

## 1. James Pether Sörling (pethers)

[GitHub profile](https://github.com/pethers)

**Research and development focus: cybersecurity, DevSecOps, public data and transparency tools.**

The public projects combine security engineering with public-information services. They cover security architecture, cloud security, DevSecOps and information security management, alongside tools for querying, analysing and presenting parliamentary data. The profile describes these projects as part of Hack23's open-source practice.

**Representative projects and technical features**

- Riksdagsmonitor / EU Parliament Monitor organize, analyse and present public data from the Swedish and European parliaments. They correspond to the riksdagsmonitor and euparliamentmonitor repositories in this sample.
- [European Parliament MCP Server](https://github.com/Hack23/European-Parliament-MCP-Server) exposes access to European Parliament data through MCP tools, combining public-data engineering with AI applications.
- ISMS-PUBLIC / CIA Compliance Manager cover publicly documented security-management policies and practices, and functionality for security assessments and compliance mapping respectively.

Together, these projects suggest a focus on the combination of security engineering and public-data products: governance and engineering safeguards sit alongside data interfaces, analysis and public presentation. The sample includes repositories labelled HTML, TypeScript and Java, spanning web presentation, service interfaces and existing systems.

**Observed scope:** 2,568 unique SHAs across 10 repositories, with sampled records from 2025-11-02 to 2026-05-31.

## 2. Roos (R00S)

[GitHub profile](https://github.com/R00S)

**Research and development focus: smart homes, device integration and kitchen automation.**

The public projects address practical device requirements within the Home Assistant ecosystem, including kitchen-device integration, home-automation extensions and an Android wake-word application. The profile also identifies custom Home Assistant integrations and applications as a development focus.

**Representative projects and technical features**

- [meater-in-local-haos](https://github.com/R00S/meater-in-local-haos) combines device integration, recipe generation, step-by-step instructions and timers for smart cooking, connecting kitchen-device information with Home Assistant services.
- [addon-tellstick-local](https://github.com/R00S/addon-tellstick-local) provides local TellStick integration for Home Assistant.
- [easy-android-ha-wakeword-app](https://github.com/R00S/easy-android-ha-wakeword-app) supplies an Android wake-word interface for Home Assistant, extending the ways users interact with home automation.

The project portfolio suggests an emphasis on application integration: combining existing devices, services and interfaces into usable functionality. The sample spans JavaScript, Python and Kotlin repositories. The largest project, meater-in-local-haos, accounts for 70.3% of repository–SHA observations, indicating a core application supported by related device interfaces.

**Observed scope:** 381 unique SHAs across six repositories, with sampled records from 2025-11-08 to 2026-05-25.

## 3. Per Johansson (1313)

[GitHub profile](https://github.com/1313)

**Research and development focus: Rust systems development, Apple platform interfaces and multimedia tools.**

The public projects emphasize Rust interfaces to Apple platform capabilities, including screen and audio capture, computational APIs and related multimedia components. The doom-fish projects featured on the profile also include a product-development collaboration tool.

**Representative projects and technical features**

- [screencapturekit-rs](https://github.com/doom-fish/screencapturekit-rs) provides Rust bindings for Apple ScreenCaptureKit, covering screen, window and audio capture on macOS.
- [accelerate-rs](https://github.com/doom-fish/accelerate-rs) wraps the Apple Accelerate C APIs through a Swift bridge, exposing computational framework functionality to Rust.
- [squad.fish](https://github.com/doom-fish/squad.fish) is a product-development collaboration project listed on the profile.

Cross-language interoperability and platform abstraction connect these projects: system frameworks are exposed through interfaces usable by Rust developers. All sampled records come from Rust-labelled repositories. The 77-repository scope and a largest-repository share of only 2.9% indicate contributions spread across multiple libraries. These observations are concentrated in May 2026 and do not establish a year-round work pattern.

**Observed scope:** 340 unique SHAs across 77 repositories, with sampled records from 2026-05-06 to 2026-05-20.

## 4. Gregor Gullwi (GregorGullwi)

[GitHub profile](https://github.com/GregorGullwi)

**Research and development focus: compilers, code generation and performance optimization.**

The profile explicitly identifies optimization, games and compilers as interests. All sampled records come from FlashCpp, making compiler development the clearest technical theme in this account's observed contributions.

**Representative projects and technical features**

- [FlashCpp](https://github.com/GregorGullwi/FlashCpp) is an experimental C++20 compiler front end focused on compilation speed and code generation. It provides Windows/MSVC and Linux/Clang development paths, together with tests that can also be run against Clang.
- computer_enhance is a pinned fork of performance-programming course code, reflecting an interest in performance engineering.
- machine_learning is described as a personal machine-learning playground, extending beyond the main compiler project.

These projects place the technical focus on foundational software and development tools, including language-feature implementation, compilation and performance. The 324 sampled SHAs all belong to FlashCpp and span 100 sampled dates, showing repeated work on one engineering project. The project describes itself as experimental rather than a production-ready compiler.

**Observed scope:** 324 unique SHAs in one repository, with sampled records from 2025-11-22 to 2026-05-27.

## 5. timmiee

[GitHub profile](https://github.com/timmiee)

**Research and development focus: browser-based 3D games, interaction systems and runtime performance.**

Waterdrop Survivor is a browser-based survival game built with THREE.js. Its public documentation describes environments, enemies, waves, particles and dialogue systems, covering both game mechanics and browser-runtime implementation.

**Representative projects and technical features**

- [Waterdrop Survivor](https://github.com/timmiee/0.2-NewVersion-Waterdrop-) is a 3D survival game centred on a waterdrop character, with exploration, enemy types, waves and narrative interaction.
- The same project's engine and sandbox provide an isolated development environment. Its documentation emphasizes object pooling, script load order and coordination between global objects.

The technical focus combines browser graphics, real-time interaction and game systems. Object pooling and the sandbox reflect attention to runtime costs and feature debugging. The sample mainly involves JavaScript-labelled repositories, with 252 unique SHAs across four repositories associated with the same game-project family.

**Observed scope:** 252 unique SHAs across four repositories, with sampled records from 2026-01-20 to 2026-03-28.

## 6. rmstdope

[GitHub profile](https://github.com/rmstdope)

**Research and development focus: Rust emulation, low-level systems and cross-platform execution.**

The sample is concentrated in NESER, a Rust game-console emulator with desktop and browser execution paths. The project centres on implementing hardware behaviour in software and supporting compatibility across environments.

**Representative projects and technical features**

- [NESER](https://github.com/rmstdope/neser) contains modules and test entry points for NES, Game Boy, GBA and SNES emulation.
- Its WebAssembly and testing tools include a browser front end, system-specific test execution and Python utilities for development checks.

The public project combines emulation behaviour, adaptation to different front ends, regression testing and toolchain maintenance. NESER accounts for 195 of 204 sampled SHAs, or 95.6%, so this account's observed activity primarily describes work on one systems project. Repository language labels are predominantly Rust, with Python also present.

**Observed scope:** 204 unique SHAs across three repositories, with sampled records from 2025-08-21 to 2026-05-27.

## 7. Pär Karlsson (feinorgh)

[GitHub profile](https://github.com/feinorgh)

**Research and development focus: procedural world generation, simulation games and systems automation.**

The public projects show two technical strands: procedural simulation and graphics in The Dark Candle, and system configuration and automation in gentoomanager. The profile describes the developer's background as systems development.

**Representative projects and technical features**

- [The Dark Candle](https://github.com/feinorgh/the-dark-candle) is a Bevy-based 3D simulation game. It generates planetary worlds from seeds, including terrain, tectonic plates, climate and biome distributions, with interactive globes and two-dimensional maps.
- [gentoomanager](https://github.com/feinorgh/gentoomanager) uses Ansible playbooks for machine configuration, Gentoo Portage settings and benchmarking workflows, with additional tools for virtual-machine resource management.

The projects combine computational modelling with engineering automation. One turns world-generation rules into interactive simulation; the other organizes deployment and performance measurement into repeatable operations. The Dark Candle contributes 116 sampled SHAs and gentoomanager 67, covering most of the account's 186 observations. Rust and Python are the main repository language labels.

**Observed scope:** 186 unique SHAs across three repositories, with sampled records from 2026-03-08 to 2026-05-28.

## 8. Hexagon

[GitHub profile](https://github.com/Hexagon)

**Research and development focus: JavaScript/TypeScript runtimes, developer tools and Rust.**

The developer's public writing covers JavaScript, TypeScript, Deno and cross-runtime development, with an emphasis on portability, limited dependencies and open-source tools. Public projects also include a Rust scheduling library and an emulator.

**Representative projects and technical features**

- Croner / Croner-rust provide cron parsing and task scheduling for JavaScript and Rust respectively.
- Pup is a TypeScript process manager for Deno, supporting development and deployment workflows.
- [Hemulator](https://github.com/Hexagon/hemulator) is a Rust multi-system emulator and the largest repository in this account's sample.

The portfolio spans foundational libraries, runtime adaptation and systems tools. Public comparisons of Node.js, Deno and Bun, together with Croner and Pup, indicate a sustained interest in portable developer tooling. In this sample, however, 113 of 173 SHAs belong to Hemulator, or 65.3%, showing a substantial Rust-emulation component in the observed work.

**Observed scope:** 173 unique SHAs across 13 repositories, with sampled records from 2025-10-28 to 2026-04-04.

## 9. Jonas Arvidson (pownas)

[GitHub profile](https://github.com/pownas)

**Research and development focus: .NET/Blazor, websites and practical applications.**

The profile describes a background in .NET systems development and continued learning in current .NET and Blazor. Public sites and the sampled repositories point mainly to web applications and practical personal projects.

**Representative projects and technical features**

- [ArvidsonFoto-MVC-NET-web](https://github.com/pownas/ArvidsonFoto-MVC-NET-web) contributes 19 sampled SHAs. This website project aligns with the profile's photography-site context and .NET development focus.
- [Rockrullarna/Webbsidan](https://github.com/Rockrullarna/Webbsidan) is described as the codebase for the Rockrullarna website, representing a practical website-maintenance setting.
- [privatekonomi](https://github.com/pownas/privatekonomi) is the account's largest sampled repository, with 103 SHAs, or 70.1%. Its repository identity and count are reported without extending the description to unverified features.

The available material suggests a focus on using the .NET ecosystem for concrete website and application requirements. C#-labelled repositories account for 124 observations, with HTML, JavaScript and other labels also present. This combines backend and web-layer work. The 147 sampled SHAs span seven repositories but are concentrated in a small number of practical projects.

**Observed scope:** 147 unique SHAs across seven repositories, with sampled records from 2025-08-20 to 2026-04-24.

## 10. hessius

[GitHub profile](https://github.com/hessius)

**Research and development focus: AI application integration, device software and user interfaces.**

MeticAI, named Metic in its current README, connects language models, MCP, a web interface and a Meticulous espresso machine. Its application features address brewing-profile generation, process interpretation and device interaction.

**Representative projects and technical features**

- [MeticAI / Metic](https://github.com/hessius/MeticAI) includes generating brewing profiles from images or text, and comparing, analysing and explaining profiles and shot curves.
- [MeticAI-web](https://github.com/hessius/MeticAI-web) is a related web repository in this sample, supporting interaction with the device and services.
- Other public tools listed on the profile include a Troponin-I-related tool, HFNOsplitter and forks of mobile-platform plugins, showing work across several application domains.

The main project integrates model capabilities into a specific device and user workflow. Its technical themes include API and service integration, visual interfaces and containerized delivery. The sample is predominantly associated with TypeScript-labelled repositories, and 124 of 135 SHAs belong to MeticAI, or 91.9%. These observations therefore mainly describe engineering iterations on that device application.

**Observed scope:** 135 unique SHAs across three repositories, with sampled records from 2025-09-18 to 2026-05-16.

## Cross-profile observations

The ten profiles show repeated development around identifiable engineering projects, with differences in the problems addressed. Foundational software focuses on language, platform and hardware interfaces. Applications integrate devices, services and user workflows. Games and simulations combine world rules, real-time interaction and graphics. Public-data projects connect information organization with security governance.

Project concentration also varies. All of GregorGullwi's sampled SHAs belong to FlashCpp, while the largest projects for rmstdope and hessius each exceed 90% of their sampled totals. The 1313 sample spans many Rust libraries, whereas pethers and Hexagon combine tools and products across several areas. These differences provide more technical context than commit counts alone.

## Limitations

This is a description of sampled commits and public information, not a complete ranking of developers in Sweden. Self-reported location does not establish nationality or residence. Commit counts do not measure ability or actual tool invocations. Technical directions are inferred from public projects rather than formal academic roles; project descriptions may postdate the observation period. Public email addresses have not been tested for deliverability.

## Supporting workbook

- [Top-100 Excel workbook](sweden-top100.xlsx): account profiles, public locations, per-agent counts, contribution roles and repository statistics.
