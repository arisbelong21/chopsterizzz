import React from 'react';
import { useLanguage } from '../locales';

const IndonesiaFlag: React.FC = () => (
  <svg
    width="15"
    height="10"
    viewBox="0 0 16 11"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    style={{
      borderRadius: '2px',
      overflow: 'hidden',
      flexShrink: 0,
      boxShadow: '0 0 0 1px rgba(255, 255, 255, 0.2)',
    }}
    aria-hidden="true"
  >
    <rect width="16" height="5.5" fill="#E11D48" />
    <rect y="5.5" width="16" height="5.5" fill="#FFFFFF" />
  </svg>
);

const UkFlag: React.FC = () => (
  <svg
    width="15"
    height="10"
    viewBox="0 0 60 30"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    style={{
      borderRadius: '2px',
      overflow: 'hidden',
      flexShrink: 0,
      boxShadow: '0 0 0 1px rgba(255, 255, 255, 0.2)',
    }}
    aria-hidden="true"
  >
    <clipPath id="uk-flag-clip">
      <rect width="60" height="30" />
    </clipPath>
    <g clipPath="url(#uk-flag-clip)">
      <path d="M0 0h60v30H0z" fill="#012169" />
      <path d="M0 0l60 30m0-30L0 30" stroke="#ffffff" strokeWidth="6" />
      <path d="M0 0l60 30m0-30L0 30" stroke="#C8102E" strokeWidth="2.5" />
      <path d="M30 0v30M0 15h60" stroke="#ffffff" strokeWidth="10" />
      <path d="M30 0v30M0 15h60" stroke="#C8102E" strokeWidth="6" />
    </g>
  </svg>
);

export const LanguageSwitcher: React.FC = () => {
  const { language, setLanguage } = useLanguage();

  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '3px',
        borderRadius: '20px',
        background: 'rgba(255, 255, 255, 0.04)',
        border: '1px solid var(--border-color)',
        backdropFilter: 'blur(8px)',
        position: 'relative',
        userSelect: 'none',
      }}
      role="group"
      aria-label="Language selection"
    >
      <button
        type="button"
        onClick={() => setLanguage('id')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '6px',
          padding: '4px 10px',
          fontSize: '0.75rem',
          fontWeight: 600,
          border: 'none',
          borderRadius: '16px',
          cursor: 'pointer',
          transition: 'all 0.2s ease',
          background: language === 'id'
            ? 'linear-gradient(135deg, #f0c96e 0%, #c99435 100%)'
            : 'transparent',
          color: language === 'id' ? '#1a1710' : 'var(--text-secondary)',
          boxShadow: language === 'id' ? '0 2px 8px rgba(227, 182, 83, 0.24)' : 'none',
        }}
        title="Bahasa Indonesia"
      >
        <IndonesiaFlag />
        <span>ID</span>
      </button>

      <button
        type="button"
        onClick={() => setLanguage('en')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '6px',
          padding: '4px 10px',
          fontSize: '0.75rem',
          fontWeight: 600,
          border: 'none',
          borderRadius: '16px',
          cursor: 'pointer',
          transition: 'all 0.2s ease',
          background: language === 'en'
            ? 'linear-gradient(135deg, #f0c96e 0%, #c99435 100%)'
            : 'transparent',
          color: language === 'en' ? '#1a1710' : 'var(--text-secondary)',
          boxShadow: language === 'en' ? '0 2px 8px rgba(227, 182, 83, 0.24)' : 'none',
        }}
        title="English"
      >
        <UkFlag />
        <span>EN</span>
      </button>
    </div>
  );
};
