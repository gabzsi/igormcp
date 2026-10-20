---
type: concept
collection: guides
tags: [igor-pro, data-folders, hierarchy, namespaces, scoping]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Wave-Architecture.md"
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
---

# Igor Pro 10 Data Folders

Data folders provide hierarchical namespacing in Igor Pro experiments, structuring waves, variables, and analysis results analogous to directories in a file system.

## Path Notation and Structure

- **Root Folder**: `root:` represents the top-level folder of every Igor experiment.
- **Subfolder Paths**: Expressed using colon delimiters: `root:Run01:RawData:`.
- **Relative Paths**:
  - `:SubFolder:` enters a child folder relative to the current active folder.
  - `::` moves up one level to the parent folder.
  - `:::WaveName` moves up two levels to reference an object.

## Essential Data Folder Commands

```text
// Create and set active data folder
NewDataFolder/O/S root:Experiment_01     // /O overwrites/reuses without error; /S sets active

// Navigate folders
SetDataFolder root:Experiment_01
SetDataFolder ::                        // Up to parent

// Query active folder
Print GetDataFolder(1)                  // Returns full path (e.g. "root:Experiment_01:")

// Move objects between folders
MoveWave root:OldFolder:w, root:NewFolder:
```

## Scoping Rules and Best Practices

1. **Active Folder Dependency**: Operations like `Make`, `Variable`, and `CurveFit` create output objects (`W_coef`, `V_chisq`) in the currently active data folder unless an absolute path prefix is provided.
2. **Explicit Qualification**: In automated scripts, always explicitly prefix wave names with data folders (e.g. `root:VoltageAligned:Run1`) to prevent accidental collisions with `root:`.
3. **Quoting Hyphens and Spaces**: Folders containing spaces or hyphens must be quoted with single quotes:
   ```text
   SetDataFolder root:'Linac-Runs':'Shot-12'
   ```

See also: [Wave Architecture](Igor-Pro-10-Wave-Architecture.md), [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md)
