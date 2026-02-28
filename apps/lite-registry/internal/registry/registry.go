package registry

import (
	"context"
	"log"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	corelister "k8s.io/client-go/listers/core/v1"
	"k8s.io/client-go/tools/cache"
)

type ServiceInfo struct {
	Lanes []string `json:"lanes"`
	Port  int32    `json:"port"`
}

type Registry struct {
	client    kubernetes.Interface
	namespace string
	resync    time.Duration

	mu        sync.RWMutex
	services  map[string]ServiceInfo
	updatedAt time.Time

	ready  atomic.Bool
	lister corelister.ServiceNamespaceLister
}

func New(client kubernetes.Interface, namespace string, resync time.Duration) *Registry {
	return &Registry{
		client:    client,
		namespace: namespace,
		resync:    resync,
		services:  make(map[string]ServiceInfo),
	}
}

func (r *Registry) Ready() bool {
	return r.ready.Load()
}

func (r *Registry) Snapshot() map[string]ServiceInfo {
	r.mu.RLock()
	defer r.mu.RUnlock()
	// Return a copy
	out := make(map[string]ServiceInfo, len(r.services))
	for k, v := range r.services {
		info := ServiceInfo{Port: v.Port, Lanes: make([]string, len(v.Lanes))}
		copy(info.Lanes, v.Lanes)
		out[k] = info
	}
	return out
}

func (r *Registry) Get(app string) (ServiceInfo, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	info, ok := r.services[app]
	return info, ok
}

func (r *Registry) UpdatedAt() time.Time {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.updatedAt
}

// Start begins watching Services and blocks until ctx is cancelled.
func (r *Registry) Start(ctx context.Context) error {
	factory := informers.NewSharedInformerFactoryWithOptions(
		r.client,
		r.resync,
		informers.WithNamespace(r.namespace),
	)

	svcInformer := factory.Core().V1().Services()
	r.lister = svcInformer.Lister().Services(r.namespace)

	handler := cache.ResourceEventHandlerFuncs{
		AddFunc:    func(_ interface{}) { r.rebuildIfReady() },
		UpdateFunc: func(_, _ interface{}) { r.rebuildIfReady() },
		DeleteFunc: func(_ interface{}) { r.rebuildIfReady() },
	}
	svcInformer.Informer().AddEventHandler(handler)

	factory.Start(ctx.Done())

	synced := factory.WaitForCacheSync(ctx.Done())
	for _, ok := range synced {
		if !ok {
			log.Println("WARNING: informer cache sync failed")
			return ctx.Err()
		}
	}

	r.rebuild()
	r.ready.Store(true)
	log.Println("registry: cache synced, ready to serve")

	<-ctx.Done()
	return ctx.Err()
}

func (r *Registry) rebuildIfReady() {
	if r.ready.Load() {
		r.rebuild()
	}
}

func (r *Registry) rebuild() {
	svcs, err := r.lister.List(labels.Everything())
	if err != nil {
		log.Printf("registry: failed to list services: %v", err)
		return
	}

	result := make(map[string]ServiceInfo)

	for _, svc := range svcs {
		appName := svc.Labels["app"]
		lane := svc.Labels["lane"]
		if appName == "" || lane == "" {
			continue
		}

		port := portFromService(svc)

		info, exists := result[appName]
		if !exists {
			info = ServiceInfo{Port: port}
		}
		info.Lanes = append(info.Lanes, lane)
		result[appName] = info
	}

	// Sort lanes for stable output
	for app, info := range result {
		sort.Strings(info.Lanes)
		result[app] = info
	}

	r.mu.Lock()
	r.services = result
	r.updatedAt = time.Now()
	r.mu.Unlock()
}

func portFromService(svc *corev1.Service) int32 {
	if len(svc.Spec.Ports) > 0 {
		return svc.Spec.Ports[0].Port
	}
	return 0
}
