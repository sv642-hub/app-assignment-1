export default function Logo({ size = 26, round = false }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden="true" className="logo">
      <rect width="28" height="28" rx={round ? 14 : 7} fill="var(--accent)" />
      <path
        d="M7 14h5l2-5 3 10 2-5h2"
        fill="none"
        stroke="#fff"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
