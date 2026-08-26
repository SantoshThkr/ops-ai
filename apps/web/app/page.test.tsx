import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';
import HomePage from './page';

describe('HomePage', () => {
  it('renders the application name', () => {
    render(<HomePage />);
    expect(screen.getByRole('heading', { name: 'OpsAI' })).toBeInTheDocument();
  });
});
