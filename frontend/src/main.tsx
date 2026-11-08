import './styles/index.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';

import { Providers } from './app/Providers';
import { createQueryClient } from './app/queryClient';
import { routes } from './app/routes';

const root = document.getElementById('root');
if (root === null) throw new Error('The page has no #root element.');

const router = createBrowserRouter(routes);
const queryClient = createQueryClient();

createRoot(root).render(
  <StrictMode>
    <Providers client={queryClient}>
      <RouterProvider router={router} />
    </Providers>
  </StrictMode>,
);
