import { Navigate } from 'react-router-dom';
import { getToken } from '../api/client';

export default function AuthGuard({ children }: { children: JSX.Element }) {
  const token = getToken();
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
}
