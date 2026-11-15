import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import EnvironmentManager from './EnvironmentManager';
import './styles.css';
import './manager.css';

const root = document.getElementById('root');
if (!root) throw new Error('Application root is unavailable.');
createRoot(root).render(
  <StrictMode>
    {document.querySelector('meta[name="lakehouse-mode"]')?.getAttribute('content') ===
    'manager' ? (
      <EnvironmentManager />
    ) : (
      <App />
    )}
  </StrictMode>,
);
