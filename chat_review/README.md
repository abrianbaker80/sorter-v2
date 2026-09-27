# Chat review packages

Each requested implementation or correction handoff has a revisioned package under `chat_review/`. A package contains the scoped patch, test/results report, and a manifest with its implementation baseline and hashes. Preserve every earlier revision; create the next `rN` directory for a correction.

Name the predecessor package when one exists. State whether a patch covers only the new slice or is cumulative from an earlier base while prior work remains uncommitted. A reviewer should be able to reconstruct the submitted changes from the patch and its recorded base.

Only review artifacts are published here. Publication does not accept or commit the implementation, approve deployment, or establish physical qualification. Give the supervising chat a commit-pinned link to the package entry report.
