/** Shared layout constants for the inspect panel's two-column comparison
 * (LHS "Sonarr Label" 2fr / zero-width seam / RHS "Content Identity" 1fr).
 *
 * `ActionBar`'s bottom row mirrors this EXACT grid template (same column
 * fractions, same gap) so its right-hand Apply Remap control group
 * pixel-matches the RHS panel's width and right edge — see inspect-v4
 * spec items 2/10 (measured requirement, RHS width ±4px / gap ≤~64px).
 * Two different grids only guarantee identical track widths when given
 * the same template + the same container width, so this is centralized
 * here rather than re-typed in both ComparisonSection and ActionBar. */
export const TWO_COLUMN_GRID_CLASS = 'grid grid-cols-[2fr_0_1fr] gap-x-6'
