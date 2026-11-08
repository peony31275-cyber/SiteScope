/**
 * Chart colors and chrome (dataviz method). Series colors are the validated dark categorical
 * slots 1-3, in fixed order (validator on the card surface #16181c: all checks pass); a single
 * series always uses slot 1. Grid and axes are recessive hairlines; text uses the text inks.
 */
export const SERIES = ['#3987e5', '#d95926', '#199e70'] as const;

export const CHART = {
  grid: '#2a2e35',
  axis: '#3a3f48',
  tick: '#8a919c',
  cursor: 'rgba(255, 255, 255, 0.04)',
  fontSize: 12,
} as const;
