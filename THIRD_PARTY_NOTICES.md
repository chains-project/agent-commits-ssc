# Third-party notices

The original pipeline installs Python dependencies separately. The integrated
dependency study vendors the small pinned packages listed below for offline replay;
all retain their upstream licenses.

| Package | Version | License family |
|---|---:|---|
| NumPy | 1.26.4 | BSD-3-Clause |
| pandas | 2.3.3 | BSD-3-Clause |
| Matplotlib | 3.9.4 | PSF-based Matplotlib license |
| seaborn | 0.13.2 | BSD-3-Clause |
| packaging | 23.0 | Apache-2.0 or BSD-2-Clause |

Synthetic fixtures contain dependency names, versions, registry responses, and
Git reference syntax needed to test the research pipeline. They are evidence
fixtures, not copied package source code. Repository and commit identifiers in
the research tables refer to public GitHub activity and are not presented as
code owned by this repository.


## Integrated study offline dependencies

Under `experiments/agent_dependency_study/vendor/`:

| Package | Version | Included license |
| --- | --- | --- |
| packaging | 21.3 | packaging-LICENSE, packaging-LICENSE.APACHE, packaging-LICENSE.BSD |
| pyparsing | 3.0.4 | pyparsing-LICENSE (MIT) |
| node-semver | 7.6.3 | semver/LICENSE (ISC) |

Small real-commit fixtures retain source repository/SHA provenance. The Java mapping
catalog records official-source references; larger cached source-tree responses are
in the optional external evidence package.

Selected official Java source files used as mapping evidence are retained under
`experiments/agent_dependency_study/research/java_high_frequency/members/` with
their original copyright/license headers and source references.

The dependency-study copy of pyparsing encodes three Unicode alias assignments with ASCII escapes. The runtime aliases and original license are preserved.
